import os
import sys, time, json, math
import minescript as m
run=sys.argv[1]
duration=float(sys.argv[2]) if len(sys.argv)>2 else 60.0
yaw_start=float(sys.argv[3]) if len(sys.argv)>3 else 0.0
pitch=float(sys.argv[4]) if len(sys.argv)>4 else 5.0
day=int(sys.argv[5]) if len(sys.argv)>5 else 6000
weather=sys.argv[6] if len(sys.argv)>6 else 'clear'
assert weather in ('clear','rain','thunder')
assert run.replace("-", "").replace("_", "").isalnum() and 5<=duration<=1200
root=os.path.abspath(os.environ.get('MCBENCH_EVIDENCE','minescript/bench-output'))+os.sep
os.makedirs(root,exist_ok=True)
m.execute("time set "+str(day))
m.execute("weather "+weather+" 1200s")
m.player_set_orientation(yaw_start, pitch)
time.sleep(8.0)
world_start=vars(m.world_info())
with open(root+"measure.properties","w") as f:
    f.write("id="+run+"\nseconds="+str(int(duration))+"\n")
import os
wait=time.monotonic()
while not os.path.exists(root+run+"-start.txt") and time.monotonic()-wait<2.0:
    time.sleep(0.01)
assert os.path.exists(root+run+"-start.txt"), "frame sampler did not start"
start=time.monotonic()
positions=[]
try:
    m.player_set_orientation(yaw_start, pitch)
    while True:
        elapsed=time.monotonic()-start
        if elapsed>=duration: break
        yaw=yaw_start+45.0-45.0*math.cos(2.0*math.pi*elapsed/duration)
        m.player_set_orientation(yaw, pitch)
        if int(elapsed)>len(positions)-1:
            positions.append({"t":elapsed,"position":m.player_position(),"orientation":m.player_orientation()})
        time.sleep(0.1)
finally:
    m.player_press_forward(False)
    m.player_press_sprint(False)
    m.player_press_jump(False)
    m.player_set_orientation(yaw_start, pitch)
with open(root+run+"-route.json","w") as f:
    json.dump({"kind":"fixed-position 90 degree out-and-back camera pan; normal world simulation","seconds":time.monotonic()-start,"positions":positions,"end_position":m.player_position(),"end_orientation":m.player_orientation(),"world_start":world_start,"world_end":vars(m.world_info()),"yaw_start":yaw_start,"pitch":pitch,"weather":weather,"day_start":day}, f)
m.echo("Finished "+run)
