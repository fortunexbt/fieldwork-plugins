package m4peak;

/** Regression tests for the live policy guard: an options file is insufficient. */
public final class PolicyTest {
    public static final class Option {
        public String value = "MINIMIZED";
        public String get() { return value; }
    }
    public static final class Options {
        public final Option option = new Option();
        public Option inactivityFpsLimit() { return option; }
    }
    public static final class Tracker {
        public String reason = "NONE";
        public int limit = 260;
        public String getThrottleReason() { return reason; }
        public int getFramerateLimit() { return limit; }
    }
    public static final class Client {
        public final Options options = new Options();
        public final Tracker tracker = new Tracker();
        public Tracker getFramerateLimitTracker() { return tracker; }
    }
    private static void rejects(Client client) throws Exception {
        try { FrameAgent.validatePolicy(client); }
        catch (IllegalStateException expected) { return; }
        throw new AssertionError("Unsafe policy accepted");
    }
    public static void main(String[] args) throws Exception {
        Client client = new Client();
        if (!FrameAgent.validatePolicy(client).contains("live_frame_limit=260")) throw new AssertionError();
        client.tracker.limit=60;
        if (!FrameAgent.validatePolicy(client).contains("live_frame_limit=60")) throw new AssertionError();
        client.options.option.value="AFK";
        rejects(client); // Reject even when currently NONE and not yet idle for a minute.
        client.options.option.value="MINIMIZED";
        for (String reason : new String[]{"SHORT_AFK","WINDOW_ICONIFIED","OUT_OF_LEVEL_MENU"}) {
            client.tracker.reason=reason; rejects(client);
        }
        System.out.println("Live policy regression checks passed (including latent AFK rejection).");
    }
}
