"""Button presentation; authorization remains in the bot handlers."""
from copy import deepcopy

def style_keyboard(markup, sensitive_allowed=False, diagnostic_running=False):
    result=deepcopy(markup)
    for row in result.get('inline_keyboard',[]):
        for button in row:
            action=button.get('callback_data','')
            if action in ('menu_restart','p2p_off','qos_flush_ips'):
                button['style']='danger'
            elif action=='p2p_on':
                button['style']='success'
            elif action.startswith('menu_') and action!='menu_close':
                button['style']='primary'
            if (action in ('menu_apps', 'menu_health', 'menu_quality') and not sensitive_allowed) or (action=='menu_diagnostic' and diagnostic_running):
                button.pop('callback_data',None)
                button['disabled']={}
    return result
