"""Validation at every write boundary; unknown facts stay empty, never guessed."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
import re

Status = Literal['待看', '感兴趣', '可投', '已投递', '面试中', 'Offer', '不合适', '已关闭']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_max_length=100000)

class Skill(Model):
    name: str = Field(max_length=120)
    level: Literal['done', 'learned', 'unknown'] = 'unknown'
    evidence: str = ''
    confirmed: bool = False

class Project(Model):
    id: str = Field(max_length=100)
    title: str = ''
    context: str = ''
    bullets: list[str] = Field(default_factory=list, max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=80)
    confirmed: bool = False

class Profile(Model):
    name: str = ''
    contact: str = ''
    years: float = Field(default=7, ge=0, le=70)
    summary: str = ''
    preferred_city: str = '深圳'
    salary_target: int = Field(default=40000, ge=0, le=1000000)
    weekly_hours: float = Field(default=8, gt=0, le=100)
    directions: list[str] = Field(default_factory=list, max_length=50)
    preferences: str = ''
    skills: list[Skill] = Field(default_factory=list, max_length=150)
    projects: list[Project] = Field(default_factory=list, max_length=80)
    education: list[str] = Field(default_factory=list, max_length=30)
    experience: list[str] = Field(default_factory=list, max_length=80)
    confirmed: bool = False
    english: dict = Field(default_factory=dict)

    @field_validator('projects')
    @classmethod
    def unique_projects(cls, value):
        ids = [p.id for p in value]
        if len(ids) != len(set(ids)) or any(not x.strip() for x in ids):
            raise ValueError('项目 id 必须非空且不重复')
        return value

class SourceConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: str = Field(max_length=100)
    kind: str = Field(max_length=40)
    name: str = Field(max_length=150)
    enabled: bool = False
    board: str = Field(default='', max_length=200)
    url: str = Field(default='', max_length=2000)
    status: str = '需要配置'
    message: str = ''

class Settings(Model):
    schedule_time: str = '09:00'
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    auto_discovery: bool = True
    email_enabled: bool = False
    email_to: str = Field(default='', max_length=254)
    ai_model: str = Field(default='', max_length=200)
    ai_base_url: str = Field(default='https://api.openai.com/v1', max_length=2000)
    use_ai: bool = False
    sources: list[SourceConfig] = Field(default_factory=list, max_length=30)

    @field_validator('schedule_time')
    @classmethod
    def time_valid(cls, v):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', v):
            raise ValueError('时间应为 HH:MM')
        return v

    @field_validator('email_to')
    @classmethod
    def email_valid(cls, v):
        if v and not re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+', v):
            raise ValueError('请输入有效的单个邮箱地址')
        return v

    @field_validator('sources')
    @classmethod
    def source_ids(cls, v):
        if len({s.id for s in v}) != len(v):
            raise ValueError('来源 id 不能重复')
        return v

class JobPatch(Model):
    company: str | None = None
    title: str | None = None
    city: str | None = None
    salary_raw: str | None = None
    experience: str | None = None
    education: str | None = None
    responsibilities: str | None = None
    requirements: str | None = None
    published_at: str | None = None
    status_validity: Literal['未知', '有效', '已关闭'] | None = None
    status: Status | None = None
    note: str | None = None
    next_action: str | None = None
    applied_at: str | None = None
    favorite: bool | None = None
    uncertain_fields: list[str] | None = None

    @field_validator('published_at', 'applied_at')
    @classmethod
    def date_valid(cls, v):
        from datetime import datetime
        if v and v != '未知':
            try:
                datetime.fromisoformat(v.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('日期应为 ISO 日期或留空；不能把发现日期当发布日期')
        return '' if v == '未知' else v

class Language(Model):
    language: Literal['zh', 'en'] = 'zh'

class Login(Model):
    password: str = Field(max_length=300)

class InterviewStart(Language):
    job_id: str
    type: Literal['HR面', '技术面', '项目深挖', '综合面'] = '综合面'

class Answer(Model):
    answer: str = Field(min_length=1, max_length=20000)

class AIClaim(Model):
    conclusion: str
    kind: Literal['明确事实', '推断', '待核实']
    job_quote: str
    evidence_refs: list[str]

class AIReport(Model):
    summary: str
    claims: list[AIClaim]
    missing_questions: list[str]

class AITurn(Model):
    question: str
    focus: str
    previous_answer_quote: str

class AITranslation(Model):
    translations: list[str]
