import json,urllib.request,urllib.error,time,os,sys
cfg=json.load(open('../config.json'))
def get(u):
    for i in range(6):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"x","Accept":"application/json"}),timeout=30))
        except urllib.error.HTTPError as e:
            if e.code==401: return None
            if e.code!=429: raise
            time.sleep(20*(i+1))
    raise RuntimeError("429")
for w in cfg["watchlist"]:
    path=f"data/{w['symbol']}.json"
    if os.path.exists(path): continue
    rows={};before=None
    for page in range(9):
        u=f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{w['pool']}/ohlcv/hour?aggregate=1&limit=1000&currency=usd&token=base"+(f"&before_timestamp={before}" if before else "")
        r=get(u)
        if r is None: print(w["symbol"],"history limit reached at page",page,flush=True); break
        lst=r["data"]["attributes"]["ohlcv_list"]
        if not lst: break
        for r in lst: rows[int(r[0])]=[float(x) for x in r[1:6]]
        before=min(int(r[0]) for r in lst)
        time.sleep(2.5)
        if len(lst)<1000: break
    data=sorted([[t]+v for t,v in rows.items()])
    json.dump(data,open(path,"w"))
    print(w['symbol'],len(data),"hours", time.strftime('%Y-%m-%d',time.gmtime(data[0][0])),"->",time.strftime('%Y-%m-%d',time.gmtime(data[-1][0])),flush=True)
