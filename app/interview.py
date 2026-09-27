"""Resumable text interviews. Local branching is explicit, never labelled AI."""
import re
from . import ai
from .models import AITurn
from .analysis import tags, evidence_bank
from .store import now, uid

def start(job, profile, resumes, language, interview_type, settings):
    resume = next((r for r in resumes if not r.get('requires_review') and r.get('language')==language), None)
    projects = [p for p in profile.get('projects',[]) if p.get('confirmed')]
    project = (resume['content']['projects'][0]['title'] if resume and resume['content'].get('projects') else projects[0]['title'] if projects else '你实际参与过的一个项目')
    requirement = (tags(job.get('requirements','')+' '+job.get('responsibilities','')) or ['岗位核心职责'])[0]
    if language=='en':
        question = f"For the {job.get('title') or 'target role'}, describe your own contribution to one relevant project and how you validated it."
        if interview_type=='HR面':
            question = f"What attracts you to the {job.get('title') or 'target role'}, and which experience supports this move?"
    elif interview_type=='HR面':
        question = f"你为什么考虑“{job.get('title') or '这个岗位'}”，哪段真实经历最能支持这次转向？"
    elif interview_type=='技术面':
        question = f"围绕岗位中的“{requirement}”，请用“{project}”说明你亲自解决的问题及验证方法。"
    else:
        question = f"请用“{project}”说明项目目标、你亲自负责的工作，以及它与“{job.get('title') or '目标岗位'}”的联系。"
    return {'id':uid(),'job_id':job['id'],'language':language,'type':interview_type,'status':'active','engine':'AI追问' if settings.get('use_ai') else '本地分支练习（无需AI）','created_at':now(),'updated_at':now(),'job_snapshot':job,'profile_snapshot':profile,'resume_id':resume['id'] if resume else None,'resume_snapshot':resume['content'] if resume else None,'messages':[{'role':'assistant','content':question,'at':now(),'focus':'项目关联与个人贡献'}],'feedback':None,'error':'','note':'未找到该语言的已确认岗位简历，使用已确认个人画像；可补简历后新开一轮。' if not resume else '使用此岗位已保存简历版本'}

def next_question(interview, answer, settings):
    lang = interview['language']
    if settings.get('use_ai'):
        result = ai.call_json(settings, '结合岗位及本岗位简历，每次只追问一个问题。必须针对最后一轮回答，previous_answer_quote为回答的逐字短引文；不要替候选人虚构答案。language='+lang, {'job':interview['job_snapshot'],'resume':interview.get('resume_snapshot'),'profile':interview['profile_snapshot'],'messages':interview['messages'][-12:]}, AITurn)
        if not result['previous_answer_quote'] or result['previous_answer_quote'] not in answer:
            raise ValueError('AI追问未引用上一轮真实回答，未保存新问题，可重试')
        return result['question'], result['focus']
    excerpt = answer.strip()[:100]
    # Deterministic adaptive branches are useful without a key. Each question quotes
    # the last answer and chooses a missing dimension, rather than replaying a list.
    if not re.search(r'\d', answer):
        return ((f'You said “{excerpt}”. What baseline or measurement supports that result? If it was not measured, explain that limitation.' if lang=='en' else f'你提到“{excerpt}”。你用什么基线或测量结果判断效果？如果当时没有测量，请说明这一局限。'), '验证与证据')
    if not re.search(r'我|本人|负责|\bI\b|my\b', answer, re.I):
        return ((f'You mentioned “{excerpt}”. Which decision or implementation was your own responsibility?' if lang=='en' else f'你提到“{excerpt}”。其中哪项决策或实现由你本人负责？'), '个人贡献边界')
    if not re.search(r'对比|基线|baseline|对照|消融|ablation',answer,re.I):
        return ((f'For “{excerpt}”, what comparison would distinguish your contribution from a change in the evaluation data?' if lang=='en' else f'针对“{excerpt}”，你做了什么对照，来排除评估数据变化带来的影响？'), '对照与可靠性')
    turn_count = sum(m['role']=='user' for m in interview['messages'])
    if turn_count % 2:
        return ((f'Given “{excerpt}”, which failure case would most change your design choice?' if lang=='en' else f'基于“{excerpt}”，哪一种失败样本最可能改变你的方案选择？'), '失败分析与取舍')
    return ((f'How would the approach in “{excerpt}” need to change for the constraints in this job?' if lang=='en' else f'把“{excerpt}”中的方法用于本岗位时，最需要重新验证的假设是什么？'), '迁移与边界')

def finish(interview):
    answers=[m['content'] for m in interview['messages'] if m['role']=='user']
    bank=evidence_bank(interview['profile_snapshot'])
    confirmed=[{'ref':k, **v} for k,v in bank.items() if v.get('confirmed')]
    evidence_text=' '.join(x['text'] for x in confirmed)
    numbers=set(re.findall(r'\d+(?:\.\d+)?%?', ' '.join(answers)))
    unverified_numbers=[n for n in sorted(numbers) if n not in evidence_text]
    english=interview['language']=='en'
    review=[]
    for index,answer in enumerate(answers,1):
        has_evidence=bool(re.search(r'\d|验证|评估|实验|baseline|evaluat|test',answer,re.I))
        has_ownership=bool(re.search(r'我|本人|负责|\bI\b|my\b',answer,re.I))
        has_structure=bool(re.search(r'首先|目标|结果|问题|because|result|goal|first',answer,re.I))
        review.append({'round':index,'answer_excerpt':answer[:200], 'accuracy':'Technical correctness needs expert verification; no automatic correctness claim.' if english else '技术内容准确性需结合代码、实验记录或专家复核；系统不凭表述判定正确。','evidence':'Measurement mentioned; confirm its provenance.' if english and has_evidence else 'Add a real baseline or acknowledge missing measurement.' if english else '提及验证或测量，仍需核对来源' if has_evidence else '补充真实基线与验证方法；没有数据时明确说明', 'structure':'Personal contribution is explicit.' if english and has_ownership else 'Clarify your own contribution.' if english else '已提及本人贡献' if has_ownership else '区分本人贡献与团队成果','expression':'Keep the goal-action-evidence-limit sequence.' if english and has_structure else 'Use goal → action → evidence → limitation.' if english else '已有目标/结果线索，建议压缩为目标—行动—证据—局限' if has_structure else '建议用目标—行动—证据—局限组织回答'})
    improvement=[]
    for p in interview['profile_snapshot'].get('projects',[]):
        if p.get('confirmed') and p.get('bullets'):
            improvement.append({'ref':'project:'+p['id'],'answer':p.get('context','')+' '+ ' '.join(p['bullets']), 'reason':'Only confirmed source facts; adapt the structure without adding metrics.' if english else '以上改进素材仅来自已确认项目原文，可按目标—行动—证据—局限重新组织。'})
    return {'engine':'Local evidence and communication review' if english else '本地证据与表达检查', 'rounds':review,'unverified_numbers':unverified_numbers,'accuracy_note':'Numbers absent from the saved profile are unverified, not necessarily incorrect. Source excerpts retain their original language.' if english else '数字未在履历中出现只代表尚未核实，不直接判错。','improved_answers':improvement[:3],'revision_list':['补充上一轮追问缺失的真实证据','检查指标的基线、测试集、测量口径','用本人决策串联项目，说明失败与局限'] if not english else ['Document evidence missing from follow-ups.','Check metric baselines, test sets and measurement methods.','Explain your decisions and limitations.'],'missing':('Add a confirmed project before rewriting answers.' if english else '没有已确认项目可用于改写，请先补充资料。') if not improvement else ''}
