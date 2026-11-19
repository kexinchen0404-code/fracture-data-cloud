"""Calibrate native IFS chart axes and read continuous PDF paths."""
from datetime import datetime, timedelta
import collections
import math
import re
import statistics

SERIES = {
    (1.0, 0.0, 0.0): ("treating_pressure", "MPa", 0),
    (0.0, 0.50196, 0.50196): ("slurry_rate", "m3/min", 1),
    (0.0, 1.0, 0.0): ("surface_proppant_concentration", "kg/m3", 2),
    (0.50196, 0.50196, 0.0): ("bottomhole_proppant_concentration", "kg/m3", 2),
}


def fit_axis(pairs):
    xs, ys = zip(*pairs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x-mx)*(y-my) for x, y in pairs) / sum((x-mx)**2 for x in xs)
    intercept = my-slope*mx
    return {"slope": slope, "intercept": intercept,
            "max_residual": max(abs(slope*x+intercept-y) for x,y in pairs),
            "anchors": pairs}


def calibrated(axis, coordinate):
    return axis["slope"]*coordinate+axis["intercept"]


def flatten_bezier(a,b,c,d,depth=0):
    # Approximate the PDF's own cubic path, never interpolate across missing data.
    dx,dy=d[0]-a[0],d[1]-a[1]
    norm=math.hypot(dx,dy)
    error=max(abs(dy*(p[0]-a[0])-dx*(p[1]-a[1]))/norm if norm else math.dist(a,p) for p in (b,c))
    if error<=.002 or depth>=20:return [d]
    ab=tuple((u+v)/2 for u,v in zip(a,b));bc=tuple((u+v)/2 for u,v in zip(b,c));cd=tuple((u+v)/2 for u,v in zip(c,d))
    abc=tuple((u+v)/2 for u,v in zip(ab,bc));bcd=tuple((u+v)/2 for u,v in zip(bc,cd))
    mid=tuple((u+v)/2 for u,v in zip(abc,bcd))
    return flatten_bezier(a,ab,abc,mid,depth+1)+flatten_bezier(mid,bcd,cd,d,depth+1)


def calibrate(page):
    words = page.extract_words()
    vertical = [l for l in page.lines if l["height"] > 200 and l["width"] < .01
                and l["stroking_color"] in (0, 0.0, (0.,0.,0.))]
    groups = collections.defaultdict(list)
    for l in vertical:
        groups[(round(l["top"],2),round(l["bottom"],2))].append(l)
    (top,bottom), axes = max(groups.items(), key=lambda item:len(item[1]))
    axis_x = sorted({round(l["x0"],3) for l in axes})
    if len(axis_x) != 3:
        raise ValueError(f"Expected 3 axes, found {axis_x}")
    left, right, _ = axis_x
    y_axes = []
    for i, x in enumerate(axis_x):
        ticks = [l["top"] for l in page.lines if l["height"] < .01
                 and 2 < l["width"] < 10 and top-.1 <= l["top"] <= bottom+.1
                 and (abs(l["x1"]-x)<.1 if i==0 else abs(l["x0"]-x)<.1)]
        labels = [w for w in words if re.fullmatch(r"\d+(?:\.\d+)?",w["text"])
                  and top-8 <= w["top"] <= bottom+2
                  and (0<x-w["x1"]<12 if i==0 else 0<w["x0"]-x<12)]
        pairs = []
        for w in labels:
            center=(w["top"]+w["bottom"])/2
            tick=min(ticks,key=lambda t:abs(t-center))
            if abs(tick-center) > 2:
                raise ValueError("Y label does not match a tick")
            pairs.append((tick,float(w["text"])))
        if len(pairs)<3:
            raise ValueError("Insufficient y calibration anchors")
        y_axes.append(fit_axis(pairs))
    times = sorted([w for w in words if re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?",w["text"])
                    and bottom < w["top"] < bottom+30],key=lambda w:w["x0"])
    dates = sorted([w for w in words if re.fullmatch(r"\d{4}-\d{2}-\d{2}",w["text"])
                    and bottom < w["top"] < bottom+40],key=lambda w:w["x0"])
    if len(times)<2 or not dates:
        raise ValueError("Insufficient time/date anchors")
    x_ticks=[l["x0"] for l in page.lines if l["width"]<.01
             and left-.1<=l["x0"]<=right+.1
             and ((abs(l["top"]-bottom)<.1 and 2<l["height"]<10)
                  or (abs(l["top"]-top)<.1 and abs(l["bottom"]-bottom)<.1))]
    day=datetime.strptime(dates[0]["text"],"%Y-%m-%d")
    anchors=[]; previous=None; base=None
    for w in times:
        center=(w["x0"]+w["x1"])/2
        x=min(x_ticks,key=lambda v:abs(v-center))
        if abs(x-center)>2:
            raise ValueError("Time label does not match a tick")
        h,m,*s=map(int,w["text"].split(":"))
        tod=h*3600+m*60+(s[0] if s else 0)
        if previous is not None and tod<previous:
            day+=timedelta(days=1)
        current=day+timedelta(seconds=tod)
        if base is None:base=current
        anchors.append((x,(current-base).total_seconds()))
        for date in dates:
            if abs((date["x0"]+date["x1"])/2-center)<3 and date["text"]!=current.strftime("%Y-%m-%d"):
                raise ValueError("Date anchors disagree")
        previous=tod
    return {"bounds":[left,top,right,bottom],"time":fit_axis(anchors),
            "time_origin_local":base.isoformat(),"y_axes":y_axes}


def read_curves(page, calibration):
    left,top,right,bottom=calibration["bounds"]
    paths=[]
    for curve in page.curves:
        color=curve.get("stroking_color")
        if not isinstance(color,tuple) or color not in SERIES or not curve["stroke"]:
            continue
        commands=curve.get("path",[])
        if any(cmd[0] not in ("m","l","c") for cmd in commands):
            continue
        name,unit,axis_no=SERIES[color]
        groups=[]
        for command in commands:
            if command[0]=="m":groups.append([])
            if groups:
                if command[0]=="c":groups[-1].extend(flatten_bezier(groups[-1][-1],*command[1:]))
                else:groups[-1].append(command[1])
        for points in groups:
            if len(points)<3:continue
            if any(not(left-.1<=x<=right+.1 and top-.1<=y<=bottom+.1) for x,y in points):
                raise ValueError(f"Curve outside plotting area: {name}")
            backtracks=[a[0]-b[0] for a,b in zip(points,points[1:]) if b[0]<a[0]-.001]
            if backtracks and max(backtracks)>.12:
                raise ValueError(f"Large x reversal in PDF path: {name}")
            clean=[]
            for x,y in points:
                if clean and abs(x-clean[-1][0])+abs(y-clean[-1][1])<1e-8:continue
                clean.append((x,y,calibrated(calibration["time"],x),
                              calibrated(calibration["y_axes"][axis_no],y)))
            paths.append({"series":name,"unit":unit,"axis":axis_no,"linewidth_pt":curve["linewidth"],
                          "points":clean,"subpixel_x_backtracks":len(backtracks),
                          "max_backtrack_seconds":max(backtracks,default=0)*calibration["time"]["slope"]})
    if {p["series"] for p in paths} != {s[0] for s in SERIES.values()}:
        raise ValueError("Not all four curves were found")
    return paths


