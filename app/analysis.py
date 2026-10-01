"""Explainable evidence matching, grounded preparation, and independent resume versions."""
import copy
import hashlib
import re
from . import ai
from .models import AIReport, AITranslation
from .store import now, uid, dump, known
from .parsing import salary_group

# Aliases map vocabulary, not proficiency. A matching keyword cannot establish depth.
TAXONOMY = {
 '检测': ['检测','目标检测','object detection','yolo'],
 '分割': ['分割','segmentation'],
 '分类': ['分类','classification'],
 '自监督学习': ['自监督','self-supervised','self supervised','masked autoencoder','masked auto-encoder','掩码自编码'],
 'MIL': ['mil','multiple instance','多实例'],
 '模型训练': ['模型训练','训练工程','model training','distributed training','分布式训练'],
 '模型部署': ['模型部署','deployment','inference','推理优化'],
 '多模态': ['多模态','multimodal','multi-modal','vlm','vision language','vision-language','qwen-vl'],
 '模型微调': ['微调','fine-tuning','fine tuning','finetuning','lora','qlora','peft'],
 '多模态微调': ['多模态微调','vlm fine-tuning','vlm fine tuning'],
 '量化': ['量化','quantization','int8','int4'],
 '蒸馏': ['蒸馏','distillation'],
 '边缘部署': ['边缘','edge','tensorrt','onnx','jetson','rknn'],
 '具身智能': ['具身','embodied','robotics','robot learning','机器人'],
 '强化学习': ['强化学习','reinforcement learning','rlhf'],
 '数据闭环': ['数据闭环','data engine','data curation','active learning','主动学习'],
 '工业视觉': ['工业视觉','industrial vision','缺陷检测','视觉质检'],
 'PyTorch': ['pytorch'],
 'Python': ['python'],
 'C++': ['c++','c＋＋'],
 '计算机视觉': ['计算机视觉','computer vision','视觉算法'],
}

def contains(text, term):
    if term.isascii() and re.fullmatch(r'[a-zA-Z]+', term):
        return bool(re.search(r'(?<![a-zA-Z])'+re.escape(term)+r'(?![a-zA-Z])', text, re.I))
    return term.lower() in text.lower()

def tags(text):
    found = {name for name, terms in TAXONOMY.items() if any(contains(text, t) for t in terms)}
    # Model names identify a topic, never a proficiency level. Qwen alone can be
    # language-only; MAE alone can mean mean absolute error, not pretraining.
    if re.search(r'(?<![a-z])qwen[\d. -]*-?vl(?![a-z])', text, re.I):
        found.add('多模态')
    if re.search(r'(?<![a-z])mae(?![a-z]).{0,24}(?:预训练|自监督|masked|pretrain)|(?:预训练|自监督|masked|pretrain).{0,24}(?<![a-z])mae(?![a-z])', text, re.I):
        found.add('自监督学习')
    # VLM usage/data annotation is not fine-tuning. Require both topics in the
    # same clause; independent requirements elsewhere in a JD cannot establish it.
    for clause in re.split(r'[\n。；;]+', text):
        multimodal = any(contains(clause, t) for t in TAXONOMY['多模态']) or bool(re.search(r'(?<![a-z])qwen[\d. -]*-?vl(?![a-z])', clause, re.I))
        if multimodal and any(contains(clause, t) for t in TAXONOMY['模型微调']):
            found.add('多模态微调')
    return [name for name in TAXONOMY if name in found]

def sentences(text):
    return [x.strip(' •-\t') for x in re.split(r'[\n。；;]+', text) if x.strip()]

def quote_for(text, tag):
    return next((s for s in sentences(text) if tag in tags(s)), '')

def requirement_priority(text, quote):
    optional=False
    for line in text.splitlines():
        if re.search(r'bonus|preferred|nice.to.have|加分项|优先条件|优先考虑',line,re.I): optional=True
        elif re.match(r'\s*(?:requirements|must.have|minimum qualifications|任职要求|必备条件)',line,re.I): optional=False
        if quote and quote in line:
            return '加分项' if optional else '要求（重要程度待确认）'
    return '要求（重要程度待确认）'

def evidence_bank(profile):
    bank = {}
    for i, skill in enumerate(profile.get('skills', [])):
        bank[f'skill:{i}'] = {'text':f"{skill['name']}：{skill.get('evidence','')}", 'confirmed':skill.get('confirmed',False), 'level':skill.get('level','unknown'), 'tags':tags(skill['name']), 'has_detail':bool(skill.get('evidence','').strip())}
    for p in profile.get('projects', []):
        bank['project:'+p['id']] = {'text': p['title']+'；'+p.get('context','')+'；'+'；'.join(p.get('bullets',[])), 'confirmed':p.get('confirmed',False), 'level':'done', 'tags':tags(' '.join(p.get('skills',[]))), 'has_detail':bool(p.get('bullets'))}
    if profile.get('confirmed'):
        bank['profile:summary'] = {'text':profile.get('summary',''), 'confirmed':True, 'level':'background', 'tags':[], 'has_detail':False}
        bank['profile:experience'] = {'text':'；'.join(profile.get('experience',[])), 'confirmed':True, 'level':'background', 'tags':[], 'has_detail':False}
    return bank

def match_job(job, profile):
    jd = '\n'.join(job.get(k, '') or '' for k in ('title','responsibilities','requirements'))
    required = tags(jd)
    bank = evidence_bank(profile)
    dimensions = []
    known_scores = []
    for tag in required:
        linked = [(ref, item) for ref,item in bank.items() if tag in item['tags']]
        done = [(r,e) for r,e in linked if e['level'] == 'done']
        practiced = [(r,e) for r,e in done if e['confirmed'] and e['has_detail']]
        learned = [(r,e) for r,e in linked if e['level'] == 'learned']
        if practiced:
            status, score, explanation = '有已确认实践证据', 100, '词汇对应已有实践；复杂度、规模与个人贡献仍需核对'
        elif done:
            status, score, explanation = '自述实践，深度待核对', None, '初始自述不等于已核验的项目证据'
        elif learned:
            status, score, explanation = '仅学习过，缺实践证据', 45, '将学习经历与已完成工作分开'
        else:
            status, score, explanation = '信息不足', None, '画像中未找到证据，不据此判定不具备能力'
        if score is not None:
            known_scores.append(score)
        quote=quote_for(jd,tag)
        dimensions.append({'requirement':tag, 'job_quote':quote, 'priority':requirement_priority(jd,quote), 'evidence':[{'ref':r, **e} for r,e in linked], 'match':status, 'score':score, 'gap':explanation})
    explicit_roles = []
    role_map = {'研究':['论文','研究','research','publication'], '算法研发':['算法','algorithm','model development'], '训练工程':['训练','training'], '部署':['部署','deployment','inference'], '数据闭环':['数据闭环','data curation','数据采集','标注'], '客户交付':['客户','交付','驻场','customer','delivery']}
    for role, terms in role_map.items():
        quotes = [s for s in sentences(job.get('responsibilities','') or jd) if any(contains(s,t) for t in terms)]
        if quotes:
            explicit_roles.append({'category':role, 'job_quote':quotes[0], 'interpretation':'推断：日常工作可能涉及'+role+'；实际时间占比需向用人经理确认'})
    salary = salary_group(job.get('salary_raw',''), profile.get('salary_target',40000))
    experience = job.get('experience','')
    years_match = re.search(r'(\d+)\s*(?:[-–~至]\s*\d+)?\s*(?:年|years?)', experience, re.I)
    level = {'requirement':experience or '未知', 'evidence':f"自述约{profile.get('years',0):g}年经验", 'match':'待确认职责范围与级别'}
    if years_match:
        level['match'] = '自述年限达到已识别的最低年限，职级仍待确认' if profile.get('years',0)>=int(years_match[1]) else '已知年限可能不足，需确认可替代的项目深度'
    projects = sorted([p for p in profile.get('projects',[]) if p.get('confirmed')], key=lambda p:len(set(tags(dump(p)))&set(required)), reverse=True)
    constraints = {'salary':salary, 'salary_raw':job.get('salary_raw','') or '未知', 'location':{'job':job.get('city','') or '未知','preferred':profile.get('preferred_city','深圳'),'result':'优先城市' if profile.get('preferred_city') and profile['preferred_city'] in job.get('city','') else '地点待确认' if not job.get('city') else '其他城市/远程，单独考虑'}, 'travel':next((s for s in sentences(jd) if any(t in s for t in ['出差','驻场','travel','onsite'])), '未披露，需确认'), 'culture':'暂无可核实证据；扁平程度、英文使用频率、自主性需沟通'}
    completeness = round(100*sum(known(job.get(k)) for k in ('company','title','city','salary_raw','experience','education','responsibilities','requirements','published_at'))/9)
    if salary['group'] == '低于目标' or job.get('status_validity') == '已关闭':
        recommendation = '暂不优先'
    elif any(x['match']=='仅学习过，缺实践证据' for x in dimensions) and not projects:
        recommendation = '补强后投递'
    elif dimensions and len(known_scores)==len(dimensions) and min(known_scores)>=80 and salary['group']=='明确符合' and constraints['location']['result']=='优先城市':
        recommendation = '优先投递'
    else:
        recommendation = '值得沟通'
    coverage = {'confirmed':sum(d['match']=='有已确认实践证据' for d in dimensions), 'total':len(dimensions), 'unknown':sum(d['match']=='信息不足' for d in dimensions), 'learned':sum(d['match']=='仅学习过，缺实践证据' for d in dimensions), 'unconfirmed':sum(d['match']=='自述实践，深度待核对' for d in dimensions)}
    return {'evidence_coverage':coverage, 'engine':'本地证据规则 v1', 'created_at':now(), 'job_id':job['id'], 'nature':explicit_roles or [{'category':'待核实','job_quote':'','interpretation':'职责信息不足，无法判断日常工作比例'}], 'dimensions':dimensions, 'project_match':[{'ref':'project:'+p['id'],'title':p['title'],'shared_skills':list(set(tags(dump(p)))&set(required)),'note':'保留原始行业背景，强调方法、验证与可迁移能力'} for p in projects[:3]], 'level':level, 'direction':{'desired':profile.get('directions',[]),'job_tags':required,'note':'主题相近不代表岗位职责相同，需核对实际研发与交付占比'}, 'constraints':constraints, 'recommendation':recommendation, 'score':round(sum(known_scores)/len(known_scores)) if known_scores else None, 'score_explanation':f'仅对有已确认实践或已记录学习证据的 {len(known_scores)}/{len(dimensions)} 项取均值；未知不计零分。这是已知条目的均值，不代表整体覆盖；请同时看已确认项/总项数。未知不代表不具备能力，也不是录用概率。', 'information_completeness':completeness, 'unconfirmed':['岗位发布时间未知'] if not job.get('published_at') else [], 'attention':questions(job, profile, dimensions), 'missing_profile':['补充并确认具体项目、个人贡献、验证方式和指标'] if not projects else []}

def questions(job, profile, dimensions=None):
    salary = job.get('salary_raw','') or '未披露'
    return [
        {'audience':'HR','priority':1,'question':f'岗位标注“{salary}”，固定税前月薪的范围是多少？额外薪数、奖金与股权是否另计？','purpose':f"确认固定月薪是否达到{profile.get('salary_target',40000):,}元，避免年包口径误差",'job_quote':salary},
        {'audience':'HR','priority':2,'question':f"工作地点标注为“{job.get('city') or '未知'}”，出差、驻场和到岗安排分别是什么？",'purpose':'确认地点及交付安排是否可接受','job_quote':job.get('city','')},
        {'audience':'用人经理','priority':1,'question':f"针对“{(job.get('responsibilities') or '尚未披露职责')[:180]}”，入职前三个月最主要的交付和验收标准是什么？",'purpose':'明确研发、训练、部署与客户交付的实际时间分配','job_quote':job.get('responsibilities','')[:180]},
        {'audience':'用人经理','priority':2,'question':'目前有哪些可用数据、标注资源、算力与部署环境？算法选型和试验优先级由谁决定？','purpose':'确认资源条件和自主决策范围；不依据公司标签推断','job_quote':''},
        {'audience':'用人经理','priority':3,'question':'日常技术评审和跨团队沟通使用哪些语言？能否举一次工程师自主推动方案的例子？','purpose':'核实英文交流比例、团队层级和技术自主性','job_quote':''},
        {'audience':'技术面试官','priority':1,'question':f"岗位涉及{', '.join((d['requirement'] for d in (dimensions or [])[:3])) or '相关算法'}时，最难的失败样本和当前基线是什么？如何做离线与线上验证？",'purpose':'将已做项目中的数据划分、误差分析和部署取舍与岗位对齐','job_quote':job.get('requirements','')[:200]},
    ]

def report(job, profile, settings):
    result = match_job(job, profile)
    result['strengths'] = [d for d in result['dimensions'] if d['match']=='有已确认实践证据']
    result['gaps'] = [d for d in result['dimensions'] if d['match'] != '有已确认实践证据']
    result['emphasis'] = result['project_match']
    if settings.get('use_ai'):
        bank = evidence_bank(profile)
        output = ai.call_json(settings, '写岗位本质与匹配分析。每个判断须提供岗位逐字引文(job_quote)和证据ID(evidence_refs)。无证据时必须为推断/待核实。summary只概括，禁止新增事实。', {'job':job, 'evidence':bank,'baseline':result}, AIReport)
        raw = '\n'.join(str(job.get(k,'')) for k in ('title','raw_text','responsibilities','requirements','salary_raw','city'))
        for claim in output['claims']:
            if claim['job_quote'] and claim['job_quote'] not in raw:
                raise ValueError('AI 分析引用了岗位中不存在的内容，结果已拒绝保存')
            if any(ref not in bank for ref in claim['evidence_refs']):
                raise ValueError('AI 分析含无效个人经历引用，结果已拒绝保存')
            if claim['kind']=='明确事实' and not claim['job_quote'] and not claim['evidence_refs']:
                raise ValueError('AI 事实结论缺少来源，结果已拒绝保存')
            if claim['kind']=='明确事实' and any(not bank[ref]['confirmed'] for ref in claim['evidence_refs']):
                raise ValueError('AI 把尚未确认的个人经历作为明确事实，结果已拒绝保存')
            if claim['kind']=='明确事实':
                # Reference existence is not entailment. Only verbatim extracts
                # can be called facts automatically. Paraphrases or new personal
                # assertions stay review-required even with a valid JD citation.
                conclusion = claim['conclusion'].strip()
                sources = []
                for ref in claim['evidence_refs']:
                    if not bank[ref]['confirmed']:
                        continue
                    evidence = bank[ref]['text'].strip()
                    sources.extend([evidence, *sentences(evidence)])
                    if ref.startswith('skill:') and '：' in evidence:
                        detail = evidence.split('：', 1)[1].strip()
                        sources.extend([detail, *sentences(detail)])
                # A job quote cannot verify a candidate claim. JD-only extracts
                # remain visible as job_quote, but their generated conclusions
                # need review; a model can put candidate assertions in any field.
                if not conclusion or conclusion not in sources:
                    claim.update(kind='待核实', requires_review=True, review_reason='引用存在不代表结论由引用支持；此改写或判断须逐项核对，不能作为个人履历事实。')
        output['summary_requires_review'] = True
        output['review_note'] = 'AI概要和非原文判断均待核对；仅核验引用存在，不自动认证候选人能力或新增经历。'
        result.update(ai_analysis=output, engine='本地证据规则 + AI（引用已检查，判断待核对）')
    else:
        result['ai_status'] = 'AI增强未启用' if ai.configured(settings) else 'AI增强待配置；当前结果为本地证据整理'
    return result

def learning_plan(job, profile):
    analysis = match_job(job, profile)
    projects = [p for p in profile.get('projects',[]) if p.get('confirmed')]
    items = []
    for d in analysis['dimensions']:
        if d['match']=='有已确认实践证据' or d.get('priority')=='加分项':
            priority, hours = '有帮助但不必优先', 1
        elif d['match']=='信息不足':
            priority, hours = '面试前必须补', 2
        else:
            priority, hours = '面试前必须补', 3
        chosen = next((p for p in projects if d['requirement'] in tags(dump(p))), projects[0] if projects else None)
        task = f"使用“{chosen['title']}”复盘数据、基线、本人决策与失败样本；" if chosen else '先补充一个真实项目中的个人贡献；'
        task += '整理与该要求的联系，缺实践时只标记为学习；做一个最小可复现对照实验，不写入已完成履历。'
        items.append({'id':hashlib.sha256((d['requirement']+(chosen['id'] if chosen else '')).encode()).hexdigest()[:16], 'priority':priority, 'topic':d['requirement'], 'goal':'核实当前掌握程度，能用证据解释关键取舍', 'job_quote':d['job_quote'], 'evidence_refs':['project:'+chosen['id']] if chosen else [], 'practice':task, 'hours':hours, 'completion':'可用3分钟讲清任务、个人行动、验证结果和局限；没有测量结果则明确尚未测量', 'status':'待完成'})
    items.append({'id':'onboarding-'+job['id'], 'priority':'入职后可以继续补','topic':'目标团队数据与部署环境','goal':'将通用方法迁移到团队真实场景','job_quote':job.get('responsibilities','')[:150],'evidence_refs':[], 'practice':'入职后了解许可范围内的数据规范、评价集和发布流程，建立一个基线复现记录','hours':4,'completion':'获得团队确认的基线与验收口径','status':'待完成'})
    before = sum(x['hours'] for x in items if x['priority']=='面试前必须补')
    return {'engine':'基于岗位证据的本地计划', 'items':items, 'weekly_hours':profile.get('weekly_hours',8), 'interview_hours':before, 'estimated_weeks':round(before/profile.get('weekly_hours',8),1), 'note':'相同能力与项目沿用同一学习项ID，优先复用已完成案例；小时数是准备估计，可按实际调整。'}

def resume_version(job, profile, settings, language='zh'):
    projects = [copy.deepcopy(p) for p in profile.get('projects',[]) if p.get('confirmed') and p.get('title') and p.get('bullets')]
    if not profile.get('confirmed') or not projects or not profile.get('name'):
        raise ai.NeedsConfiguration('待补充履历：请确认个人画像、姓名，以及至少一个含真实个人贡献的项目；不会生成虚构简历。')
    wanted = set(tags(job.get('requirements','')+' '+job.get('responsibilities','')+' '+job.get('title','')))
    # Tailoring only reorders confirmed facts. It does not promote a learned skill or
    # manufacture metrics. Example: deployment JD brings the TensorRT project first.
    projects.sort(key=lambda p:len(wanted&set(tags(dump(p)))), reverse=True)
    for p in projects:
        p['bullets'].sort(key=lambda b:len(wanted&set(tags(b))), reverse=True)
    content = {'name':profile['name'],'headline':'','contact':profile.get('contact',''), 'summary':profile.get('summary',''), 'skills':[s['name'] for s in profile.get('skills',[]) if s.get('confirmed') and s.get('level')=='done'], 'projects':[{'id':p['id'],'title':p['title'],'context':p.get('context',''),'bullets':p['bullets']} for p in projects], 'education':profile.get('education',[]), 'experience':profile.get('experience',[]), 'language':language}
    content['skills'].sort(key=lambda x:0 if set(tags(x))&wanted else 1)
    changes = [{'before':' → '.join(p['title'] for p in profile['projects'] if p.get('confirmed')), 'after':' → '.join(p['title'] for p in projects), 'reason':'按岗位要求与已确认项目的关键词交集排序；行业背景和事实原文完整保留'}, {'before':' → '.join(s['name'] for s in profile.get('skills',[]) if s.get('confirmed') and s.get('level')=='done'), 'after':' → '.join(content['skills']), 'reason':'优先展示岗位相关技能；仅学习过、尚未接触和未确认技能不写成实践能力'}]
    review = False
    if language=='en':
        if not ai.configured(settings):
            raise ai.NeedsConfiguration('英文版本待配置 AI 翻译接口。中文证据版可独立生成、预览和下载。')
        paths, strings = [], []
        def visit(obj, path=()):
            if isinstance(obj, dict):
                for key,val in obj.items():
                    if key not in ('id','language','contact','name'):
                        visit(val, path+(key,))
            elif isinstance(obj, list):
                for i,val in enumerate(obj):
                    visit(val,path+(i,))
            elif isinstance(obj,str) and obj:
                paths.append(path); strings.append(obj)
        visit(content)
        output = ai.call_json(settings, '逐项忠实翻译为英文，顺序与数量必须不变。禁止添加职责、指标、技能、学位或背景。保留所有数字、型号和项目背景。', {'strings':strings}, AITranslation)
        if len(output['translations']) != len(strings):
            raise ValueError('翻译条目数量不一致，未保存英文简历')
        for path, original, translated in zip(paths, strings, output['translations']):
            if sorted(re.findall(r'\d+(?:\.\d+)?',original)) != sorted(re.findall(r'\d+(?:\.\d+)?',translated)):
                raise ValueError('翻译改变了数值，未保存英文简历')
            node=content
            for part in path[:-1]: node=node[part]
            node[path[-1]]=translated
            changes.append({'before':original,'after':translated,'reason':'英文翻译，需逐项核对后才能下载'})
        review = True
    return {'id':uid(), 'job_id':job['id'], 'created_at':now(), 'language':language,'content':content,'changes':changes,'requires_review':review,'profile_snapshot':copy.deepcopy(profile),'job_snapshot':copy.deepcopy(job),'engine':'已确认事实重排'+(' + AI翻译待核对' if review else ''),'missing_questions':[x for x,missing in [('补充教育经历',not profile.get('education')),('补充联系信息',not profile.get('contact')),('补充工作单位及起止时间',not profile.get('experience'))] if missing]}

def stories(job, profile):
    projects = [p for p in profile.get('projects',[]) if p.get('confirmed') and p.get('bullets')]
    if not projects or not profile.get('confirmed'):
        raise ai.NeedsConfiguration('请先确认至少一个真实项目；其他岗位分析、沟通问题与学习计划可以先完成。')
    wanted=set(tags(job.get('requirements','')+' '+job.get('responsibilities','')))
    projects.sort(key=lambda p:len(set(tags(dump(p)))&wanted), reverse=True)
    return {'engine':'已确认经历整理', 'introduction':profile.get('summary','')+' 我会重点介绍'+ '、'.join(p['title'] for p in projects[:2])+'。', 'cases':[{'ref':'project:'+p['id'],'title':p['title'],'background':p.get('context',''),'confirmed_facts':p['bullets'],'structure':['背景与目标：解释原行业场景，不隐瞒项目背景','本人行动：从上述已确认贡献中选择关键决策','验证与结果：仅使用真实记录中的指标，没有则明确缺少量化','迁移与局限：说明与本岗位的相似问题和不同条件'],'needs_follow_up':'补充未记录的规模、对照实验、失败案例与个人边界，不能编造'} for p in projects[:3]], 'note':'不足2–3个已确认项目时只展示实际数量。'}
