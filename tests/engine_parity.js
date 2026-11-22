// Checks that the browser engine (app/engine.js) and the Python engine (engine/analyze_csv.py)
// give identical results. Run the Python analyzer on both samples first, then: node tests/engine_parity.js
const fs=require('fs'); process.chdir(require('path').join(__dirname,'..')); const E=require(require('path').join(__dirname,'..','app','engine.js'));
function load(f){const L=fs.readFileSync(f,'utf8').trim().split('\n');const h=L[0].split(',');return {cols:h,rows:L.slice(1).map(l=>{const v=l.split(',');const o={};h.forEach((k,i)=>o[k]=v[i]);return o;})};}
for (const [f,opt,py] of [
 ['samples/superstore_orders.csv',{date:'Order Date',metric:'Sales',agg:'sum',period:'M'},'results/superstore_orders_packets.json'],
 ['samples/olist_orders_sample.csv',{date:'order_date',metric:'order_value',agg:'sum',period:'M'},'results/olist_orders_sample_packets.json']]){
  const {cols,rows}=load(f); const prof=E.profile(rows,cols);
  const dims=prof.filter(c=>c.type==='category'&&c.distinct>=2&&c.distinct<=100&&c.column!==opt.metric).slice(0,6).map(c=>c.column);
  const res=E.analyze(rows,{...opt,dims});
  const P=JSON.parse(fs.readFileSync(py,'utf8'));
  let maxdiff=0, topmatch=0, n=0;
  for(const pk of res.packets){const q=P.find(x=>x.month===pk.month); if(!q) {console.log('missing',pk.month);continue;}
    maxdiff=Math.max(maxdiff,Math.abs(pk.change-q.change),Math.abs((pk.z_score??0)-(q.z_score??0)));
    for(const d of dims){n++; const a=pk.drivers[d].pushed_with_the_change[0], b=q.drivers[d].pushed_with_the_change[0]; if((a&&a.segment)===(b&&b.segment)) topmatch++;}}
  console.log(f, 'types', prof.map(c=>c.column+':'+c.type).join(' '), '\n periods', res.packets.length, 'vs', P.length, 'max diff', maxdiff.toFixed(6), 'top driver match', topmatch+'/'+n);
}
