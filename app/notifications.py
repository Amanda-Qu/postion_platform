"""Persisted at-most-once email attempts; uncertainty is visible after interruption."""
from email.message import EmailMessage
import os
import smtplib
import ssl

def configured():
    return bool(os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM'))

def deliver(digest, jobs, settings):
    if not configured():
        raise ValueError('待配置 SMTP_HOST / SMTP_FROM 及需要的认证环境变量')
    if not settings.get('email_to'):
        raise ValueError('请先设置推送收件邮箱')
    message=EmailMessage()
    message['From']=os.environ['SMTP_FROM']
    message['To']=settings['email_to']
    message['Subject']=f"求职工作台 · {digest['date']} · {len(jobs)} 个新机会"
    # Deterministic Message-ID helps mail clients identify retries, but the worker
    # itself reserves sending before SMTP and never automatically repeats it.
    message['Message-ID']=f"<{digest['id']}@personal-career-desk.local>"
    lines=['以下为首次推荐的岗位。首次发现时间不代表发布时间。','']
    for job in jobs:
        lines.extend([f"{job.get('company') or '未知公司'} | {job.get('title') or '待核对岗位'} | {job.get('city') or '未知地点'}",f"薪资原文：{job.get('salary_raw') or '未知'}；发布日期：{job.get('published_at') or '未知'}"])
        lines.extend(s['url'] for s in job.get('sources',[]) if s.get('url'))
        lines.append('')
    message.set_content('\n'.join(lines))
    host=os.environ['SMTP_HOST']; port=int(os.getenv('SMTP_PORT','587'))
    security=os.getenv('SMTP_SECURITY','starttls')
    if security not in ('starttls','ssl'):
        raise ValueError('邮件只支持 STARTTLS 或 SSL 加密连接')
    context=ssl.create_default_context()
    cls=smtplib.SMTP_SSL if security=='ssl' else smtplib.SMTP
    kwargs={'context':context} if security=='ssl' else {}
    with cls(host,port,timeout=30,**kwargs) as client:
        if security=='starttls': client.starttls(context=context)
        if os.getenv('SMTP_USER'): client.login(os.environ['SMTP_USER'],os.getenv('SMTP_PASSWORD',''))
        client.send_message(message)
