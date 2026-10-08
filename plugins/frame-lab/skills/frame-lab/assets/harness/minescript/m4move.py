import os
import sys,time,json,os,math
import minescript as m
run=sys.argv[1]; duration=int(sys.argv[2]); kind=sys.argv[3]
assert run.replace('-','').replace('_','').isalnum() and 5<=duration<=1200
assert kind in ('walk','sprint','fly','swim','shuttle')
root=os.path.abspath(os.environ.get('MCBENCH_EVIDENCE','minescript/bench-output'))+os.sep
os.makedirs(root,exist_ok=True)
m.execute('time set 6000');m.execute('weather clear 1200s')
yaw,pitch=m.player_orientation();m.player_set_orientation(yaw,pitch)
time.sleep(8)
initial=m.player_position();world=vars(m.world_info())
with open(root+'measure.properties','w') as f:f.write('id='+run+'\nseconds='+str(duration)+'\n')
wait=time.monotonic()
while not os.path.exists(root+run+'-start.txt') and time.monotonic()-wait<2:time.sleep(.01)
assert os.path.exists(root+run+'-start.txt')
start=time.monotonic();positions=[]
try:
 m.player_press_sprint(kind in ('sprint','fly','swim'));m.player_press_forward(True)
 while time.monotonic()-start<duration:
  elapsed=time.monotonic()-start
  if kind=='shuttle':
   phase=elapsed%13.6
   m.player_set_orientation(yaw+(180 if 4<=phase<8 or phase>=10.8 else 0),pitch)
   m.player_press_sprint(phase>=8)
  if int(elapsed)>=len(positions):
   positions.append({'t':elapsed,'position':m.player_position(),'world':vars(m.world_info())})
  time.sleep(.1)
finally:
 m.player_press_forward(False);m.player_press_sprint(False);m.player_press_jump(False)
 m.player_set_orientation(yaw,pitch)
end=m.player_position()
with open(root+run+'-route.json','w') as f:
 json.dump({'kind':kind+'; time based forward input; normal simulation','seconds':time.monotonic()-start,'initial':initial,'end_position':end,'distance':math.dist(initial,end),'sampled_path_length':sum(math.dist(a['position'],b['position']) for a,b in zip(positions,positions[1:])),'yaw':yaw,'pitch':pitch,'positions':positions,'world_start':world,'world_end':vars(m.world_info())},f)
m.execute('tick query');m.echo('M4 movement complete '+run)
