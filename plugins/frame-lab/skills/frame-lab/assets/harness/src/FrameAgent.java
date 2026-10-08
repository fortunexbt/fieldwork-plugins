package m4peak;

import java.lang.instrument.*;
import java.security.ProtectionDomain;
import java.nio.file.*;
import java.io.*;
import java.lang.reflect.*;
import java.util.*;
import org.objectweb.asm.*;
import static m4peak.FrameSink.*;

/** Task-only frame-production sampler. No network, OS input, or account access. */
public final class FrameAgent {
    private static volatile String hook = "not installed";
    private static Path root;
    private static String lastId = "";

    public static void premain(String argument, Instrumentation inst) throws Exception {
        root = Paths.get(argument).toAbsolutePath().normalize();
        Files.createDirectories(root);
        Path ownJar = Paths.get(FrameAgent.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        inst.appendToBootstrapClassLoaderSearch(new java.util.jar.JarFile(ownJar.resolveSibling("frame-sink.jar").toFile()));
        inst.addTransformer(new ClassFileTransformer() {
            @Override public byte[] transform(Module module, ClassLoader loader, String name,
                    Class<?> type, ProtectionDomain domain, byte[] bytes) {
                if (!"net/minecraft/client/Minecraft".equals(name)) return null;
                try {
                    ClassReader reader = new ClassReader(bytes);
                    final boolean[] hasRenderFrame = {false};
                    reader.accept(new ClassVisitor(Opcodes.ASM9) {
                        @Override public MethodVisitor visitMethod(int access, String name,
                                String desc, String sig, String[] exceptions) {
                            if (name.equals("renderFrame") && desc.equals("(Z)V")) hasRenderFrame[0] = true;
                            return null;
                        }
                    }, ClassReader.SKIP_CODE);
                    String target = hasRenderFrame[0] ? "renderFrame" : "runTick";
                    ClassWriter writer = new ClassWriter(reader, 0);
                    reader.accept(new ClassVisitor(Opcodes.ASM9, writer) {
                        @Override public MethodVisitor visitMethod(int access, String name,
                                String desc, String sig, String[] exceptions) {
                            MethodVisitor mv = super.visitMethod(access, name, desc, sig, exceptions);
                            if (!name.equals(target) || !desc.equals("(Z)V")) return mv;
                            hook = "Minecraft." + target + "(boolean) return; CPU frame-production interval, including limiter";
                            return new MethodVisitor(Opcodes.ASM9, mv) {
                                @Override public void visitInsn(int opcode) {
                                    if (opcode == Opcodes.RETURN) {
                                        super.visitVarInsn(Opcodes.ALOAD, 0);
                                        super.visitMethodInsn(Opcodes.INVOKESTATIC, "m4peak/FrameSink", "frame", "(Ljava/lang/Object;)V", false);
                                    }
                                    super.visitInsn(opcode);
                                }
                                @Override public void visitMaxs(int stack, int locals) {
                                    super.visitMaxs(Math.max(stack, 1), locals);
                                }
                            };
                        }
                    }, 0);
                    return writer.toByteArray();
                } catch (Throwable e) { writeError(e); return null; }
            }
        });
        Thread controller = new Thread(FrameAgent::control, "M4-frame-sampler-control");
        controller.setDaemon(true);
        controller.start();
    }

    private static void control() {
        String current = null;
        String requested = null;
        while (true) {
            try {
                if (current != null && !recording) { finish(current); current = null; }
                Path job = root.resolve("measure.properties");
                if (Files.exists(job) && current == null) {
                    Properties p = new Properties();
                    try (Reader r = Files.newBufferedReader(job)) { p.load(r); }
                    String id = p.getProperty("id", "");
                    if (!id.equals(lastId) && id.matches("[A-Za-z0-9_-]{1,100}")) {
                        lastId = id;
                        requested = id;
                        // A launcher restart must never replay or overwrite an old capture.
                        if (Files.exists(root.resolve(id + "-start.txt")) ||
                                Files.exists(root.resolve(id + "-done.txt"))) continue;
                        int seconds = Integer.parseInt(p.getProperty("seconds", "60"));
                        if (seconds < 5 || seconds > 1200) throw new IllegalArgumentException("duration out of bounds");
                        if (client == null || hook.equals("not installed")) throw new IllegalStateException("frame hook not ready");
                        String preflight = validatePolicy(client);
                        count = 0; unfocusedFrames = 0;
                        deadline = System.nanoTime() + seconds * 1000000000L;
                        current = id;
                        Files.writeString(root.resolve(id + "-start.txt"), "hook=" + hook + "\nseconds=" + seconds + "\n" + preflight);
                        recording = true;
                    }
                }
                Thread.sleep(200);
            } catch (InterruptedException e) { return; }
            catch (Throwable e) {
                writeError(e);
                if (requested != null) try { Files.writeString(root.resolve(requested + "-refused.txt"), e.toString() + "\n"); } catch (IOException ignored) { }
                recording = false; current = null;
            }
        }
    }

    /** Check the live client's setting, not an options file that may be stale. */
    static String validatePolicy(Object mc) throws ReflectiveOperationException {
        Object options = mc.getClass().getField("options").get(mc);
        Object option = options.getClass().getMethod("inactivityFpsLimit").invoke(options);
        Object value = option.getClass().getMethod("get").invoke(option);
        if (!"MINIMIZED".equals(String.valueOf(value)))
            throw new IllegalStateException("REFUSED: scripted capture requires MINIMIZED inactivity policy; live value=" + value);
        Object tracker = mc.getClass().getMethod("getFramerateLimitTracker").invoke(mc);
        Object reason = tracker.getClass().getMethod("getThrottleReason").invoke(tracker);
        if (!"NONE".equals(String.valueOf(reason)))
            throw new IllegalStateException("REFUSED: client is throttled; live reason=" + reason);
        Object limit = tracker.getClass().getMethod("getFramerateLimit").invoke(tracker);
        return "live_inactivity_policy=" + value + "\nlive_throttle_reason=" + reason + "\nlive_frame_limit=" + limit + "\n";
    }

    private static void finish(String id) throws IOException {
        int n = count;
        try (BufferedWriter out = Files.newBufferedWriter(root.resolve(id + "-frames.csv"))) {
            out.write("frame,nanotime,interval_ns\n");
            for (int i=0; i<n; i++) out.write(i + "," + times[i] + "," + (i == 0 ? 0 : times[i]-times[i-1]) + "\n");
        }
        Files.writeString(root.resolve(id + "-done.txt"), "frames=" + n + "\nunfocused_frames=" + unfocusedFrames + "\nframe_sink_error=" + error + "\nbuffer_full=" + (n == times.length) + "\nhook=" + hook + "\n");
    }

    private static void writeError(Throwable e) {
        try { Files.writeString(root.resolve("agent-error.txt"), e.toString() + "\n", StandardOpenOption.CREATE, StandardOpenOption.APPEND); }
        catch (Exception ignored) { }
    }
}
