#!/usr/bin/env python3
"""Record a macOS preflight, then start one in-client route. No daemon/listener."""
import argparse,hashlib,json,pathlib,re,subprocess
p=argparse.ArgumentParser()
p.add_argument('run');p.add_argument('--game',type=pathlib.Path,required=True)
p.add_argument('--seconds',type=int,default=120)
p.add_argument('--route',choices=['pan','walk','sprint','fly','swim','shuttle'],default='pan')
p.add_argument('--controller',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]/'build/controller')
p.add_argument('--evidence',type=pathlib.Path)
p.add_argument('--min-idle',type=float,default=75)
p.add_argument('--yaw',type=float,default=0);p.add_argument('--pitch',type=float,default=5)
p.add_argument('--day',type=int,default=6000);p.add_argument('--weather',choices=['clear','rain','thunder'],default='clear')
a=p.parse_args()
assert re.fullmatch('[A-Za-z0-9_-]{1,100}',a.run) and 5<=a.seconds<=1200
game=a.game.resolve();root=(a.evidence or game/'minescript/bench-output').resolve();root.mkdir(parents=True,exist_ok=True)
assert not any(root.glob(a.run+'-*')),'Use a unique run ID; preserve rejected preflights'
options=dict(l.split(':',1) for l in (game/'options.txt').read_text().splitlines() if ':' in l)
assert options.get('inactivityFpsLimit')=='"minimized"','REFUSED: change inactive policy to Minimized and restart'
state=json.loads(subprocess.check_output([str(a.controller),'state']))
windows=[w for w in state['windows'] if w.get('kCGWindowOwnerName')=='java' and 'Minecraft' in w.get('kCGWindowName','')]
assert len(windows)==1 and state['front_pid']==windows[0]['kCGWindowOwnerPID'],'REFUSED: one visible, foreground Minecraft client required'
env=subprocess.check_output(['top','-l','3','-s','1','-o','cpu','-n','12','-stats','pid,command,cpu,mem'],text=True)
(root/(a.run+'-environment.txt')).write_text(env)
idle=[float(x) for x in re.findall(r'CPU usage:.*?([0-9.]+)% idle',env)]
swaps=[int(x) for x in re.findall(r'\((\d+)\) swapouts',env)]
gate={'idle_last_two':idle[-2:],'swapouts_last_two':swaps[-2:],'minimum_idle':a.min_idle}
gate['passed']=len(idle)>=3 and min(idle[-2:])>=a.min_idle and not any(swaps[-2:])
context={'run':a.run,'seconds':a.seconds,'route':a.route,'window':state,'environment_gate':gate,'options':options,'window_bounds_units':'CGWindowBounds are macOS points; verify actual framebuffer separately'}
context['sampler_jars']={name:hashlib.sha256((a.controller.resolve().parent/name).read_bytes()).hexdigest() for name in ('frame-agent.jar','frame-sink.jar') if (a.controller.resolve().parent/name).exists()}
context['mods']=[{'name':f.name,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in sorted((game/'mods').glob('*.jar'))]
iris=game/'config/iris.properties'
if iris.exists():context['iris']=iris.read_text()
context['shader_settings']={f.name:f.read_text() for f in (game/'shaderpacks').glob('*.zip.txt')}
(root/(a.run+'-context.json')).write_text(json.dumps(context,indent=2))
assert gate['passed'],'REFUSED: environment gate failed; retain record and retry later with a new run ID'
command='\\m4pan '+a.run+' '+str(a.seconds)+' '+str(a.yaw)+' '+str(a.pitch)+' '+str(a.day)+' '+a.weather if a.route=='pan' else '\\m4move '+a.run+' '+str(a.seconds)+' '+a.route
for action in [('key','t'),('text',command),('key','Return')]:subprocess.run([str(a.controller),*action],check=True)
print('Requested '+a.run+'. The agent independently verifies the live policy before recording.')
