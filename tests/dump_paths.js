/* Evaluates the trajectory functions out of static/index.html and prints the
   positions they produce, so a Python test can hold them against dsp.py. */
const fs=require('fs'), path=require('path');
const html=fs.readFileSync(path.join(__dirname,'..','static','index.html'),'utf8');
const js=html.match(/<script>\n([\s\S]*)\n<\/script>/)[1];
const grab=(from,to)=>js.slice(js.indexOf(from), js.indexOf(to))
  .replace(/^(const|let) /gm,'var ');
global.THREE={Vector3:function(x,y,z){this.x=x;this.y=y;this.z=z;}};
var meta={orbit:43,elev:25,vocal_path:{arc:55,elev:9}};
eval(grab('const R=4.1;','/* drag to spin'));
const ts=JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  wander: ts.map(t=>{const p=posAt(t);return [p.x,p.y,p.z];}),
  stage:  ts.map(t=>{const p=stagePosAt(t);return [p.x,p.y,p.z];}),
}));
