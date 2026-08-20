const fs=require('fs');
const html=fs.readFileSync(require('path').join(__dirname,'..','static','index.html'),'utf8');
const js=html.match(/<script>\n([\s\S]*)\n<\/script>/)[1];
let fail=0;
const ok=(c,m)=>{ console.log((c?'  ok   ':'  FAIL ')+m); if(!c) fail++; };

// ---- 1. every $("#id") and getElementById target must exist in the markup ----
const ids=new Set([...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]));
const used=new Set([...js.matchAll(/\$\("#([A-Za-z0-9_-]+)"\)/g)].map(m=>m[1]));
const missing=[...used].filter(i=>!ids.has(i));
console.log('\nDOM references');
ok(missing.length===0, `all ${used.size} $("#id") lookups resolve` + (missing.length?` — missing: ${missing}`:''));

// ---- 2. ring geometry ----
console.log('\nRing geometry');
const grab=(from,to)=>js.slice(js.indexOf(from), js.indexOf(to))
  .replace(/^(const|let) /gm,'var ').replace(/\bconst (CX|CY|RAD|GAP)=/g,'var $1=');
eval(grab('const CX=100','let ring='));
const seg=(a,b)=>arcPath(a,b);
ok(seg(0,0.25).startsWith('M 100.00 16.00'), 'arc starts at 12 o\'clock');
ok(seg(0,0.25).includes('A 84 84 0 0 1'), 'quarter arc uses small-arc flag');
ok(seg(0,0.75).includes('A 84 84 0 1 1'), 'three-quarter arc uses large-arc flag');
ok(seg(0,0)==='' , 'zero-length arc renders nothing');
ok(seg(0,0.0002)==='' , 'sub-threshold arc renders nothing (no NaN path)');
const d=seg(0,1); ok(!/NaN/.test(d), 'full circle produces no NaN');
// segment layout: weights must tile the circle exactly
const plan=[{id:'decode',weight:10},{id:'mix',weight:40},{id:'convolve',weight:15},{id:'encode',weight:20}];
const total=plan.reduce((s,x)=>s+x.weight,0); let at=0;
plan.forEach(s=>{s.a0=at; at+=s.weight/total; s.a1=at;});
ok(Math.abs(at-1)<1e-9, 'segments tile exactly one turn');
ok(Math.abs(plan[1].a1-plan[1].a0 - 40/85)<1e-9, 'segment width equals its measured share');
plan.forEach(s=>ok(!/NaN/.test(arcPath(s.a0+GAP,s.a1-GAP)), `segment ${s.id} path is finite`));

// ---- 3. detail formatter over real payloads seen from the server ----
console.log('\nDetail line');
eval(grab('const fmtTime=','/* ---------- library'));
eval(grab('function detailText','\nfunction showStatus'));
const cases=[
 ['decode',{src_sr:44100,channels:2,seconds:578,resampling:false,work_sr:44100}],
 ['separate',{model:'htdemucs',device:'mps',percent:42.5,stems:4}],
 ['envelopes',{stem:'vocals',index:3,stems:4,rate:20}],
 ['mix',{xover:200,beta:0.92}],
 ['mix',{drifting:'drums'}],
 ['convolve',{blocks:9816,total:12448,batch:409,block:4096,hop:2048,taps:256,sr:44100,positions:1897,rate:8482}],
 ['encode',{fmt:'FLAC',bits:24,sr:44100,mb:109.5}],
];
for(const [st,d] of cases){
  const t=detailText(st,d);
  ok(t && !/undefined|NaN|null/.test(t), `${st.padEnd(9)} → ${t}`);
}
ok(!/undefined|NaN/.test(detailText('convolve',{})), 'convolve with an empty payload is still safe');
ok(detailText('nonsense',{note:'hi'})==='hi', 'unknown stage falls back to its note');

// ---- 4. orientation: the browser's axis mapping vs the HRIR convention ----
// SOFA azimuth +90 deg is the LEFT ear; tests/test_orientation.py pins that
// against the actual SADIE data. three.js +x is screen RIGHT. So a positive
// azimuth must be drawn at NEGATIVE x, and front (+x in SOFA) at negative z.
console.log('\nOrientation');
global.THREE={Vector3:function(x,y,z){this.x=x;this.y=y;this.z=z;}};
var meta={orbit:43,elev:25};
eval(grab('const R=4.1;','/* drag to spin'));
let wrongSide=0,tested=0,wrongDepth=0,depthTested=0;
for(let t=0;t<900;t+=1.3){
  const sc=43/50,TAU=2*Math.PI;
  const azd=180*(0.55*Math.sin(TAU*t/(97*sc))+0.30*Math.sin(TAU*t/(61*sc)+1.7)
                +0.15*Math.sin(TAU*t/(37*sc)+4.1));
  const p=posAt(t), sinaz=Math.sin(azd*Math.PI/180), cosaz=Math.cos(azd*Math.PI/180);
  if(Math.abs(sinaz)>0.08){ tested++; if(Math.sign(p.x)===Math.sign(sinaz)) wrongSide++; }
  if(Math.abs(cosaz)>0.08){ depthTested++; if(Math.sign(p.z)===Math.sign(cosaz)) wrongDepth++; }
}
ok(wrongSide===0, `left-ear azimuths draw to screen-left (${tested} times sampled)`);
ok(wrongDepth===0, `front azimuths draw toward the front wall (${depthTested} times sampled)`);

// The drift stems are amplitude-panned, not HRTF'd: spatialize_cli gives
// channel 0 (left) cos(ang) with ang=(pan+1)*PI/4, so pan>0 is the RIGHT
// channel and must therefore be drawn at positive x.
eval(grab('const DRIFT_X','const listener='));
meta.drift={vocals:{depth:.18,span:.28,phase:4.0,period:900}};
let driftWrong=0,driftTested=0;
for(let t=0;t<900;t+=1.3){
  const pan=.28*Math.sin(2*Math.PI*t/900+4.0), x=driftX('vocals',t);
  if(Math.abs(pan)<0.02) continue;
  driftTested++; if(Math.sign(x)!==Math.sign(pan)) driftWrong++;
}
ok(driftWrong===0, `pan-right draws to screen-right (${driftTested} times sampled)`);
ok(driftX('nosuchstem',10)===0, 'a stem with no drift metadata stays put');

// ---- 5. reflections: image-source mirroring and visibility gating ----------
console.log('\nWall reflections');
global.THREE.Vector3=function(x,y,z){this.x=x||0;this.y=y||0;this.z=z||0;
  this.copy=function(p){this.x=p.x;this.y=p.y;this.z=p.z;return this;};};
eval(grab('const RW=6.4','const wallMeshes='));
const P={x:1.5,y:-0.4,z:-2.0};
const byName=n=>WALLS.find(w=>w.n===n);
ok(WALLS.every(w=>w.ax>=0), 'every wall resolves to a single axis');
ok(Math.abs(wallDist(byName('right'),P)-(RW-1.5))<1e-9, 'distance to the right wall');
ok(Math.abs(wallDist(byName('left'),P)-(RW+1.5))<1e-9, 'distance to the left wall');
ok(Math.abs(wallDist(byName('floor'),P)-(RH-0.4))<1e-9, 'distance to the floor');
ok(Math.abs(wallDist(byName('front'),P)-(RD-2.0))<1e-9, 'distance to the front wall');
// a mirrored source must sit as far outside the wall as the source is inside,
// and must not move on the other two axes
for(const w of WALLS){
  const im=imagePos(w,P), a=AXIS[w.ax];
  const inside=Math.abs(w.v-P[a]), outside=Math.abs(im[a]-w.v);
  const others=AXIS.filter(x=>x!==a).every(x=>Math.abs(im[x]-P[x])<1e-9);
  ok(Math.abs(inside-outside)<1e-9 && others && Math.abs(im[a])>Math.abs(w.v)-1e-9,
     `${w.n.padEnd(5)} image is mirrored across the plane and outside the room`);
}
console.log(fail? `\n${fail} CHECK(S) FAILED\n` : '\nall checks passed\n');
process.exit(fail?1:0);
