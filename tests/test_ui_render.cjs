// Dependency-free render contracts, not a substitute for browser visual QA.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js','utf8').replace(/\nboot\(\);\s*$/, '\n');
function ui(){const context=vm.createContext({document:{addEventListener(){}},window:{addEventListener(){}},location:{origin:'http://localhost'},URL,Date,Intl,setTimeout,clearTimeout});vm.runInContext(source,context);return expression=>vm.runInContext(expression,context);}

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
