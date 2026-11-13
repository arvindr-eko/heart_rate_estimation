import evalkit as E
BANDS=[("30-40",30,40),("40-60",40,60),("60-100",60,100),("100-160",100,160),("160+",160,300)]
def by_band(res, rows, label):
    o,_=E.summarize(res, rows); print(f"{label:28s} ALL  {E.fmt(o)}")
    for n,lo,hi in BANDS:
        rr=[r for r in rows if lo<=float(r["bpm"])<hi]
        if rr:
            o,_=E.summarize(res, rr); print(f"{'':28s} {n:7s} {E.fmt(o)}")
