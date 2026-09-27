#!/usr/bin/osascript -l JavaScript
// Print "x,y,w,h,windowID" of the iPhone Mirroring window via CGWindowList (no Automation permission needed).
ObjC.import('CoreGraphics');
ObjC.import('Foundation');
var list = $.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, $.kCGNullWindowID);
var arr = ObjC.deepUnwrap(ObjC.castRefToObject(list));
var out = "";
for (var i = 0; i < arr.length; i++) {
  var w = arr[i];
  if (w.kCGWindowOwnerName === "iPhone Mirroring" && w.kCGWindowLayer === 0) {
    var b = w.kCGWindowBounds;
    if (b.Height > 200) { out = b.X + "," + b.Y + "," + b.Width + "," + b.Height + "," + w.kCGWindowNumber; break; }
  }
}
out;
