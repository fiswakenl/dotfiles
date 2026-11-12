import json,statistics as st
from pathlib import Path
d=json.load(open(Path.home()/'.claude/gv-keep/detect_bench.json'))
rows=d['rows']; med=lambda x: round(st.median(x),2) if x else None
for s in (0.25,0.4):
    R=[r for r in rows if r['silence']==s]; D=[r for r in R if r['done']]; C=[r for r in R if not r['done']]
    print(f"\n=== тишина детектора {s}: кусков {len(R)}, договорил {len(D)}, продолжал {len(C)}")
    lag=[r['final_at']-r['true_end'] for r in R]
    print(' готовый текст после конца слова: медиана',med(lag),'мин',round(min(lag),2),'макс',round(max(lag),2))
    print(' черновик в момент паузы уже равен готовому:',sum(r['draft']==r['final'] for r in R),'/',len(R))
    full=[r['draft_full_at']-r['true_end'] for r in R if r['draft_full_at']]
    print(' черновик стал полным раньше готового:',len(full),'/',len(R),'медиана после конца слова',med(full))
    print(' задержка вопроса: медиана',med([r['turn_final']['s'] for r in R]),'макс',max(r['turn_final']['s'] for r in R))
    for key,name in (('turn_final','по готовому'),('turn_draft','по черновику')):
        tp=sum(r[key]['v']=='complete' for r in D); fp=sum(r[key]['v']=='complete' for r in C)
        print(f"  «мысль закончена?» {name}: договорил→закрыл {tp}/{len(D)}, продолжал→закрыл зря {fp}/{len(C)}")
    print(' вердикт по черновику = вердикту по готовому:',sum((r['turn_draft']['v']=='complete')==(r['turn_final']['v']=='complete') for r in R),'/',len(R))
    print(' EOT договорил :',sorted(r['eot'] for r in D))
    print(' EOT продолжал:',sorted(r['eot'] for r in C))
    for th in (0.1,0.2,0.3,0.4,0.5):
        print(f"   порог {th}: закрыл верно {sum(r['eot']>=th for r in D)}/{len(D)}, зря {sum(r['eot']>=th for r in C)}/{len(C)}")
    print(' паузы «продолжал» по длине:',sorted(r['gap'] for r in C))
    print(' зря закрытые (по готовому):',[(r['gap'],r['turn_final']['p'],r['eot'],r['final'][-28:]) for r in C if r['turn_final']['v']=='complete'])
    print(' не закрытые, хотя договорил:',[(r['turn_final']['v'],r['turn_final']['p'],r['eot'],r['final'][-28:]) for r in D if r['turn_final']['v']!='complete'])
