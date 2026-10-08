import Foundation
import AppKit
import ApplicationServices

// Optional macOS input helper. Uses existing permissions only.
let args = Array(CommandLine.arguments.dropFirst())
let keys: [String: CGKeyCode] = ["a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,"b":11,"q":12,"w":13,"e":14,"r":15,"y":16,"t":17,"1":18,"2":19,"3":20,"4":21,"6":22,"5":23,"=":24,"9":25,"7":26,"-":27,"8":28,"0":29,
"o":31,"u":32,"i":34,"p":35,"Return":36,"l":37,"j":38,"k":40,"n":45,"m":46,"Tab":48,"space":49,"Delete":51,"Escape":53,"super":55,"shift":56,"control":59,"F2":120,"F3":99,"F11":103,"Up":126,"Down":125,"Left":123,"Right":124,"/":44,".":47,";":41,"[":33,"]":30,"\\":42,"'":39,",":43,"`":50]
func emit(_ value: Any) { if let data = try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]), let str=String(data:data,encoding:.utf8) {print(str)} }
func requireInput() { if !AXIsProcessTrusted() { fputs("Accessibility permission unavailable; no input sent.\n",stderr);exit(2) } }
func key(_ name: String,_ down: Bool,_ flags: CGEventFlags = []) { guard let code=keys[name],let event=CGEvent(keyboardEventSource:nil,virtualKey:code,keyDown:down) else {return};event.flags=flags;event.post(tap:.cghidEventTap) }
func tap(_ name:String) { key(name,true);Thread.sleep(forTimeInterval:0.03);key(name,false) }
func releaseAll() { for k in ["w","a","s","d","space","shift","control","super"] {key(k,false)} }
func move(_ x:Double,_ y:Double) { CGEvent(mouseEventSource:nil,mouseType:.mouseMoved,mouseCursorPosition:CGPoint(x:x,y:y),mouseButton:.left)?.post(tap:.cghidEventTap) }
func click(_ x:Double,_ y:Double,_ right:Bool) { let point=CGPoint(x:x,y:y);move(x,y);Thread.sleep(forTimeInterval:0.03);let b:CGMouseButton=right ? .right:.left;CGEvent(mouseEventSource:nil,mouseType:right ? .rightMouseDown:.leftMouseDown,mouseCursorPosition:point,mouseButton:b)?.post(tap:.cghidEventTap);Thread.sleep(forTimeInterval:0.04);CGEvent(mouseEventSource:nil,mouseType:right ? .rightMouseUp:.leftMouseUp,mouseCursorPosition:point,mouseButton:b)?.post(tap:.cghidEventTap) }
guard let action=args.first else {exit(1)}
if action == "state" {
 let windows=(CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []).filter {w in let name=w[kCGWindowOwnerName as String] as? String ?? "";return name == "java" || name == "Prism Launcher"}
 emit(["accessibility":AXIsProcessTrusted(),"screen_capture":CGPreflightScreenCaptureAccess(),"front_pid":NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0,"windows":windows]);exit(0)
}
requireInput()
if action != "focus" && action != "release" {
 let front=NSWorkspace.shared.frontmostApplication
 let name=front?.localizedName ?? ""
 guard name == "java" || name == "Prism Launcher" else {fputs("Refusing input outside Minecraft/Prism.\n",stderr);exit(3)}
 if name == "java" {
  let windows=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
  let hasGame=windows.contains { ($0[kCGWindowOwnerPID as String] as? Int32) == front?.processIdentifier && ($0[kCGWindowName as String] as? String ?? "").contains("Minecraft") }
  guard hasGame else {fputs("Refusing input: focused Java window is not Minecraft.\n",stderr);exit(3)}
 }
}
if action == "key", args.count>1, args[1] == "super+q" {
 let target=NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
 guard target == "java" || target == "Prism Launcher" else {fputs("Refusing quit outside the task game/launcher.\n",stderr);exit(3)}
}
if action == "focus",args.count>1,let pid=Int32(args[1]) { guard let app=NSRunningApplication(processIdentifier:pid), !app.isTerminated else {exit(3)};app.activate(options: [.activateIgnoringOtherApps]);Thread.sleep(forTimeInterval:0.15);guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {exit(3)} }
else if action == "key",args.count>1 { let parts=args[1].split(separator:"+").map(String.init); var flags:CGEventFlags=[]; if parts.contains("super") {flags.insert(.maskCommand)};if parts.contains("shift"){flags.insert(.maskShift)};if parts.contains("control"){flags.insert(.maskControl)};for mod in parts.dropLast() {key(mod,true,flags)};if let k=parts.last {key(k,true,flags);Thread.sleep(forTimeInterval:0.03);key(k,false,flags)};for mod in parts.dropLast().reversed() {key(mod,false)} }
else if action == "hold",args.count>2,let duration=Double(args[2]),duration<=60 {let names=args[1].split(separator:"+").map(String.init);for k in names {key(k,true)};Thread.sleep(forTimeInterval:duration);for k in names.reversed(){key(k,false)}}
else if action == "click",args.count>2,let x=Double(args[1]),let y=Double(args[2]) {click(x,y,args.count>3 && args[3]=="right")}
else if action == "move",args.count>2,let x=Double(args[1]),let y=Double(args[2]) {move(x,y)}
else if action == "scroll",args.count>1,let lines=Int32(args[1]) {CGEvent(scrollWheelEvent2Source:nil,units:.line,wheelCount:1,wheel1:lines,wheel2:0,wheel3:0)?.post(tap:.cghidEventTap)}
else if action == "text",args.count>1 {
 let shifted:[String:String]=["_":"-",":":";","{":"[","}":"]","\"":"'","@":"2","!":"1","#":"3","$":"4","%":"5","^":"6","&":"7","*":"8","(":"9",")":"0","+":"=","<":",",">":".","?":"/","~":"`","|":"\\"]
 for ch in args[1] {
  let raw=String(ch);let lower=raw.lowercased();let shift=shifted[raw] != nil || (raw != lower);let name=raw == " " ? "space":(shifted[raw] ?? lower)
  if shift {key("shift",true,.maskShift)}
  key(name,true,shift ? .maskShift:[]);Thread.sleep(forTimeInterval:0.008);key(name,false,shift ? .maskShift:[])
  if shift {key("shift",false)}
  Thread.sleep(forTimeInterval:0.008)
 }
}
else if action == "look",args.count>2,let dx=Double(args[1]),let dy=Double(args[2]) {
 for _ in 0..<20 {let position=CGEvent(source:nil)?.location ?? CGPoint(x:900,y:570);if let e=CGEvent(mouseEventSource:nil,mouseType:.mouseMoved,mouseCursorPosition:CGPoint(x:position.x+dx/20,y:position.y+dy/20),mouseButton:.left){e.setDoubleValueField(.mouseEventDeltaX,value:dx/20);e.setDoubleValueField(.mouseEventDeltaY,value:dy/20);e.post(tap:.cghidEventTap)};Thread.sleep(forTimeInterval:0.02)}
}
else if action == "route",args.count>1 {
 let plan=try JSONSerialization.jsonObject(with:Data(contentsOf:URL(fileURLWithPath:args[1]))) as! [String:Any]
 let seconds=plan["seconds"] as! Double;guard seconds>=1 && seconds<=1200 else {exit(3)}
 let events=(plan["events"] as? [[String:Any]] ?? []).sorted{($0["at"] as! Double)<($1["at"] as! Double)}
 let start=ProcessInfo.processInfo.systemUptime
 defer {releaseAll()}
 for e in events { let target=start+(e["at"] as! Double);while ProcessInfo.processInfo.systemUptime<target {Thread.sleep(forTimeInterval:min(0.02,target-ProcessInfo.processInfo.systemUptime))};if let k=e["key"] as? String,let down=e["down"] as? Bool {key(k,down)};if let x=e["x"] as? Double,let y=e["y"] as? Double {move(x,y)} }
 while ProcessInfo.processInfo.systemUptime<start+seconds {Thread.sleep(forTimeInterval:0.02)}
 emit(["route_seconds":ProcessInfo.processInfo.systemUptime-start,"events":events.count])
}
else if action == "release" {releaseAll()}
else {fputs("Unknown controller command.\n",stderr);exit(1)}

Thread.sleep(forTimeInterval:0.2)
