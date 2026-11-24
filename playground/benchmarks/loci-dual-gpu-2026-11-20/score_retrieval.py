#!/usr/bin/env python3
"""Score exact-source retrieval responses without changing raw model records."""
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); rows=[json.loads(x) for x in p.read_text().splitlines() if x.strip()]
for r in rows:
 a=r['response'].lower()
 r['score']={
  'safe_abstention_top1':r['retrieved_k']==1 and 'unavailable' in a and not any(x in a for x in ('f-8','f-9','18.4','b3')),
  'exact_facts_top4':r['retrieved_k']==4 and all(x in a for x in ('18.4','f-9','b3')) and 'f-8' not in a,
 }
groups={}
for r in rows:
 k=(r['model_key'],r['device'],str(r['retrieved_k']));groups.setdefault(k,[]).append(r)
summary=[]
for (m,d,k),rs in sorted(groups.items()):
 field='safe_abstention_top1' if k=='1' else 'exact_facts_top4'
 summary.append({'model_key':m,'device':d,'retrieved_k':int(k),'criterion':field,'successes':sum(r['score'][field] for r in rs),'n':len(rs)})
out=p.with_name(p.stem.replace('_trials','_scoring')+'.json');out.write_text(json.dumps({'rubric':'top-1 must abstain if target ID absent; top-4 must return all three exact target facts and not substitute distractor F-8. Source ID need not be repeated in answer.','groups':summary},indent=2)+'\n');print(out)
