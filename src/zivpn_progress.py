"""Read-only diagnostics with private Telegram drafts and bounded cancellation."""
import secrets
import threading
import time
from zivpn_rich import RichReport, heading, paragraph, details, footer

class Diagnostics:
    def __init__(self,api_call,send,providers,allowed):
        self.api_call=api_call;self.send=send;self.providers=providers;self.allowed=allowed
        self.lock=threading.Lock();self.jobs={}
    def running(self,chat_id):
        with self.lock:return chat_id in self.jobs
    def start(self,chat_id,user_id,chat_type):
        if chat_type!='private' or chat_id!=user_id or not self.allowed(user_id):return 'Accès au diagnostic refusé.'
        with self.lock:
            if chat_id in self.jobs:return 'Un diagnostic est déjà en cours. Utilisez /annuler.'
            if len(self.jobs)>=4:return 'Diagnostics occupés ; réessayez dans un instant.'
            job={'id':secrets.randbits(31) or 1,'user':user_id,'cancel':threading.Event()}
            self.jobs[chat_id]=job
        threading.Thread(target=self._run,args=(chat_id,job),daemon=True).start()
        return None
    def cancel(self,chat_id,user_id,draft_id=None):
        with self.lock:
            job=self.jobs.get(chat_id)
            if not job or job['user']!=user_id or (draft_id is not None and job['id']!=draft_id):return False
            job['cancel'].set();return True
    def stopped(self,event):
        chat=event.get('chat',{})
        if chat.get('type')=='private':return self.cancel(chat.get('id'),chat.get('id'),event.get('draft_id'))
        return False
    def _run(self,chat_id,job):
        lines=['Diagnostic en cours — aucune modification du VPN.'];sections=[];draft_supported=True;fallback_id=None;fallback_attempted=False
        try:
            for index,(title,provider) in enumerate(self.providers,1):
                if job['cancel'].is_set() or not self.allowed(job['user']):break
                lines.append(f'{index}/{len(self.providers)} — {title}…')
                text='\n'.join(lines)[-3500:]
                if draft_supported:
                    result=self.api_call('sendMessageDraft',{'chat_id':chat_id,'draft_id':job['id'],'text':text,'can_stop':True,'keep_on_stop':False})
                    if result and not result.get('ok') and result.get('error_code') in (400,404):draft_supported=False
                if not draft_supported and (fallback_id is not None or not fallback_attempted):
                    payload={'chat_id':chat_id,'text':text,'reply_markup':{'inline_keyboard':[[{'text':'Arrêter le diagnostic','callback_data':f'diagnostic_cancel:{job["id"]}','style':'danger'}]]}}
                    if fallback_id is not None:payload['message_id']=fallback_id
                    fallback_attempted=True
                    result=self.api_call('sendMessage' if fallback_id is None else 'editMessageText',payload)
                    if result and result.get('ok') and isinstance(result.get('result'),dict):fallback_id=result['result'].get('message_id',fallback_id)
                try:value=provider(job['cancel'])
                except Exception as error:value='Vérification indisponible : '+type(error).__name__
                sections.append(details(title,[paragraph(str(value)[:2500])]))
                lines[-1]=f'{index}/{len(self.providers)} — {title} : terminé.'
            if not self.allowed(job['user']):return
            status='Diagnostic arrêté' if job['cancel'].is_set() else 'Diagnostic terminé'
            report=RichReport([heading(status),paragraph(time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()))]+sections+[footer('Diagnostic en lecture seule. Arrêter interrompt les vérifications restantes ; aucun VPN redémarré.')])
            # The final report persists after the temporary draft disappears.
            self.send(report,chat_id=chat_id)
            if fallback_id is not None:self.api_call('editMessageText',{'chat_id':chat_id,'message_id':fallback_id,'text':status,'reply_markup':{'inline_keyboard':[]}})
        except Exception as error:
            print(f"[Diagnostic Task Error] {type(error).__name__}", flush=True)
            if self.allowed(job['user']):
                try:
                    self.send('❌ Diagnostic interrompu par une erreur. Consultez les journaux du bot.', chat_id=chat_id)
                except Exception as notice_error:
                    print(f"[Diagnostic Notice Error] {type(notice_error).__name__}", flush=True)
        finally:
            with self.lock:
                if self.jobs.get(chat_id) is job:self.jobs.pop(chat_id,None)
