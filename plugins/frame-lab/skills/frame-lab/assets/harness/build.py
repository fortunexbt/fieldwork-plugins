#!/usr/bin/env python3
"""Build the thin agent without downloading or bundling Minecraft."""
import argparse,hashlib,pathlib,shutil,subprocess,tempfile
p=argparse.ArgumentParser()
p.add_argument('--jdk',type=pathlib.Path,required=True)
p.add_argument('--asm',type=pathlib.Path,required=True)
a=p.parse_args();root=pathlib.Path(__file__).resolve().parent
assert hashlib.sha256(a.asm.read_bytes()).hexdigest()=='ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb','ASM9.10.1 hash mismatch'
out=root/'build';out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='frame-bench-build-') as tmp:
 classes=pathlib.Path(tmp)/'classes';classes.mkdir()
 sources=[root/'src/FrameAgent.java',root/'src/FrameSink.java',root/'tests/PolicyTest.java']
 subprocess.run([str(a.jdk/'bin/javac'),'-cp',str(a.asm),'-d',str(classes),*[str(s) for s in sources]],check=True)
 subprocess.run([str(a.jdk/'bin/java'),'-cp',str(classes)+':'+str(a.asm),'m4peak.PolicyTest'],check=True)
 manifest=pathlib.Path(tmp)/'manifest.mf'
 # Fabric rejects duplicate ASM classes even when the copies have identical bytes.
 # Share the exact existing launcher-classpath URL; never copy or embed ASM.
 manifest.write_text('Manifest-Version: 1.0\nPremain-Class: m4peak.FrameAgent\nClass-Path: '+a.asm.resolve().as_uri()+'\n\n')
 subprocess.run([str(a.jdk/'bin/jar'),'--create','--file',str(out/'frame-sink.jar'),'-C',str(classes),'m4peak/FrameSink.class'],check=True)
 members=sorted(str(x.relative_to(classes)) for x in (classes/'m4peak').glob('FrameAgent*.class'))
 subprocess.run([str(a.jdk/'bin/jar'),'--create','--file',str(out/'frame-agent.jar'),'--manifest',str(manifest),'-C',str(classes),members[0],*sum((['-C',str(classes),m] for m in members[1:]),[])],check=True)
if shutil.which('swiftc'):
 subprocess.run(['swiftc',str(root/'src/controller.swift'),'-O','-o',str(out/'controller')],check=True)
for file in sorted(out.iterdir()):print(file.name,hashlib.sha256(file.read_bytes()).hexdigest())
