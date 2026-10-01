// Dependency-free render contracts, not a substitute for browser visual QA.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js','utf8').replace(/\nboot\(\);\s*$/, '\n');
function ui(overrides={}){const context=vm.createContext({document:{addEventListener(){}},window:{addEventListener(){}},location:{origin:'http://localhost'},URL,URLSearchParams,Date,Intl,setTimeout,clearTimeout,...overrides});vm.runInContext(source,context);return expression=>vm.runInContext(expression,context);}

test('salary filters send stored API categories while keeping their display labels',async()=>{
  const rows=[
    ['confirmed','明确符合','固定月薪符合'],
    ['possible','可能符合','薪资可能符合'],
    ['unknown','信息不足','薪资待核实'],
    ['below','低于目标','薪资低于目标'],
  ];
  const requests=[];
  const run=ui({fetch:async path=>{
    const query=new URL(path,'http://localhost').searchParams;
    requests.push(query);
    const category=query.get('salary_group');
    return {ok:true,json:async()=>({jobs:rows.some(([,stored])=>stored===category)
      ?[{id:'salary-test',title:'测试薪资岗位',salary_group:category}]:[]})};
  }});
  for(const [key,stored,label] of rows){
    const html=await run(`renderJobs({salary_group:'${key}',q:'测试',city:'深圳',status:'可投',sort:'salary'})`);
    const query=requests.at(-1);
    assert.equal(query.get('salary_group'),stored);
    assert.equal(query.get('q'),'测试');assert.equal(query.get('city'),'深圳');
    assert.equal(query.get('status'),'可投');assert.equal(query.get('sort'),'salary');
    assert.match(html,/测试薪资岗位/);assert.match(html,/共 1 个岗位/);
    assert.match(html,new RegExp(`<option value="${key}" selected>${label}</option>`));
  }
  await run('renderJobs({salary_group:""})');
  assert.equal(requests.at(-1).has('salary_group'),false);
});

function sourceEditorRun(rows,edits={}){
  const elements=rows.map((row,index)=>({dataset:{source:String(index)},querySelector(selector){
    const key=selector.match(/data-key=([^\]]+)/)[1];
    const identifier=row.kind==='lever'?(row.site||row.slug||''):(row.board||row.slug||'');
    const defaults={...row,board:identifier};
    return {value:(edits[index]?.[key]??defaults[key])||'',checked:edits[index]?.enabled??!!row.enabled};
  }}));
  const run=ui({document:{addEventListener(){},querySelectorAll(){return elements;}}});
  run(`state.settings={sources:${JSON.stringify(rows)}}`);
  return run;
}

test('source collection preserves unrelated identifiers and saved connection evidence',()=>{
  const rows=[
    {id:'gh',kind:'greenhouse',name:'Greenhouse',board:'figureai',site:'legacy-unused',enabled:true,status:'已连接',message:'原有连接结果',last_fetched:'2026-10-01'},
    {id:'lever',kind:'lever',name:'Lever',board:'legacy-board',site:'palantir',enabled:true,status:'已连接',region:'eu'},
    {id:'portal',kind:'career_portal',name:'人工入口',url:'https://company.example/jobs',status:'人工查看'},
  ];
  const result=JSON.parse(JSON.stringify(sourceEditorRun(rows)('collectSources()')));
  for(const [index,row] of rows.entries()){
    for(const [key,value] of Object.entries(row))assert.deepEqual(result[index][key],value);
  }
  assert.equal(Object.hasOwn(result[2],'board'),false);
  assert.equal(Object.hasOwn(result[2],'site'),false);
  const leverHtml=ui()(`sourceConfig(${JSON.stringify(rows[1])},1)`);
  assert.match(leverHtml,/data-key="board" value="palantir"/);
});

test('editing or switching a source writes only the applicable board or site',()=>{
  const rows=[
    {id:'gh',kind:'greenhouse',name:'Greenhouse',board:'old-gh',site:'unused-site'},
    {id:'lever',kind:'lever',name:'Lever',board:'unused-board',site:'old-lever'},
    {id:'switch',kind:'greenhouse',name:'Switch',board:'old-board'},
  ];
  const edits={0:{board:'new-gh'},1:{board:'new-lever'},2:{kind:'lever',board:'new-site'}};
  const result=JSON.parse(JSON.stringify(sourceEditorRun(rows,edits)('collectSources()')));
  assert.equal(result[0].board,'new-gh');assert.equal(result[0].site,'unused-site');
  assert.equal(result[1].site,'new-lever');assert.equal(result[1].board,'unused-board');
  assert.equal(result[2].kind,'lever');assert.equal(result[2].site,'new-site');
  assert.equal(result[2].board,'old-board');
});

test('saving a legacy slug-only source preserves its fallback until edited',()=>{
  const rows=[
    {id:'gh',kind:'greenhouse',name:'Legacy Greenhouse',slug:'figureai',status:'已连接'},
    {id:'lever',kind:'lever',name:'Legacy Lever',site:'',slug:'palantir',status:'已连接'},
  ];
  const saved=JSON.parse(JSON.stringify(sourceEditorRun(rows)('collectSources()')));
  assert.equal(saved[0].slug,'figureai');assert.equal(Object.hasOwn(saved[0],'board'),false);
  assert.equal(saved[1].site,'');assert.equal(saved[1].slug,'palantir');
  const changed=JSON.parse(JSON.stringify(sourceEditorRun(rows,{0:{board:'new-gh'},1:{board:'new-lever'}})('collectSources()')));
  assert.equal(changed[0].board,'new-gh');assert.equal(changed[1].site,'new-lever');
  assert.match(ui()(`sourceConfig(${JSON.stringify(rows[0])},0)`),/data-key="board" value="figureai"/);
  assert.match(ui()(`sourceConfig(${JSON.stringify(rows[1])},1)`),/data-key="board" value="palantir"/);
});

test('clearing an identifier cannot silently reuse an older slug fallback',()=>{
  const rows=[
    {id:'gh-slug',kind:'greenhouse',name:'Slug Greenhouse',slug:'old-gh'},
    {id:'lever-slug',kind:'lever',name:'Slug Lever',site:'',slug:'old-lever'},
    {id:'gh-board',kind:'greenhouse',name:'Greenhouse',board:'current-gh',slug:'older-gh'},
    {id:'lever-site',kind:'lever',name:'Lever',site:'current-site',slug:'older-site'},
    {id:'switch',kind:'greenhouse',name:'Switch',slug:'old-gh'},
  ];
  const edits={0:{board:''},1:{board:''},2:{board:''},3:{board:''},4:{kind:'lever',board:''}};
  const saved=JSON.parse(JSON.stringify(sourceEditorRun(rows,edits)('collectSources()')));
  for(const row of saved){
    assert.equal(row[row.kind==='greenhouse'?'board':'site'],'');
    assert.equal(Object.hasOwn(row,'slug'),false);
  }
});

test('old successful result stays visible through retry and failure',()=>{
  const run=ui();
  for(const status of ['queued','running','failed','blocked']){
    const html=run(`taskResult({tasks:[{kind:'questions',language:'zh',status:'${status}',result:{question:'先前保存的问题'},error:'本次失败原因'}]},'questions','沟通问题','说明')`);
    assert.match(html,/先前保存的问题/);assert.match(html,/保留上次成功结果/);assert.match(html,/本次失败原因/);
  }
});
test('selected historic resume is stale even when latest task is fresh',()=>{
  const run=ui();
  const html=run(`renderResumes({resumes:[{id:'old',language:'zh',stale:true,stale_reason:'历史版本待更新',content:{},created_at:'2026-10-01'}],tasks:[{kind:'resume',language:'zh',stale:false}]})`);
  assert.match(html,/历史版本待更新/);
});
test('source editor preserves dedicated and manual kinds',()=>{
  const run=ui();
  for(const kind of ['tokenfab','extremevision','career_portal']){
    const html=run(`sourceConfig({kind:'${kind}',name:'测试来源',ingestion_status:'portal_pending'},0)`);
    assert.match(html,new RegExp(`value="${kind}" selected`));
    assert.match(html,/自动接入待实现/);
  }
});
test('job card makes partial evidence and unknown salary visible',()=>{
  const html=ui()(`jobCard({id:'x',title:'视觉算法',salary_group:'信息不足',city:'深圳',constraint_fit:{code:'unknown',label:'地点符合 · 固定月薪待核实'},analysis:{evidence_coverage:{confirmed:1,total:5}}})`);
  assert.match(html,/固定月薪待核实/);assert.match(html,/实践证据 1\/5 项/);assert.match(html,/发布时间未知/);assert.doesNotMatch(html,/100%/);
});
test('AI review flags have human labels and visible caution',()=>{
  const html=ui()(`renderAnalysis({job:{},analysis:{ai_analysis:{summary:'待核对总结',summary_requires_review:true,review_note:'请核对 AI 事实',claims:[{requires_review:true,review_reason:'证据不足'}]}}})`);
  assert.match(html,/请核对 AI 事实/);assert.match(html,/需要核对的原因/);assert.match(html,/AI 总结待逐项核对/);
});
test('untrusted source text and task content stay escaped',()=>{
  const run=ui();
  assert.doesNotMatch(run(`sourceConfig({name:'<img src=x onerror=evil()>',kind:'career_portal'},0)`),/<img/);
  assert.match(run(`taskResult({tasks:[{kind:'questions',result:{question:'<script>evil()</script>'},status:'completed'}]},'questions','标题','说明')`),/&lt;script&gt;/);
});

test('company discovery shows setup, exact preview, unknown leads and escapes search data',()=>{
  const html=ui()(`companyDiscoveryPanel({scope:'线索不是岗位',configured:false,setup:'BRAVE_SEARCH_API_KEY 未配置',queries:['深圳 Python 招聘'],last_run:{at:'2026-10-01',errors:[],candidates:[{id:'one',url:'https://company.example/careers',title:'<script>evil()</script>',snippet:'未核实',query:'深圳 Python 招聘',verification:'unknown',review_status:'pending'}]}})`);
  assert.match(html,/BRAVE_SEARCH_API_KEY/);assert.match(html,/深圳 Python 招聘/);
  assert.match(html,/确认关键词并搜索一次/);assert.match(html,/公司官网归属：未知/);
  assert.match(html,/在招岗位／城市／薪资：未知/);assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/disabled/);
});
