# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2014 IBM Corporation
# Copyright 2015-2019 Lenovo
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This SCGI server provides a http wrap to confluent api
# It additionally manages httprequest console sessions
import base64
try:
    import Cookie
except ModuleNotFoundError:
    import http.cookies as Cookie
import confluent.auth as auth
import confluent.config.attributes as attribs
import confluent.consoleserver as consoleserver
import confluent.forwarder as forwarder
import confluent.exceptions as exc
import confluent.log as log
import confluent.messages
import confluent.core as pluginapi
import confluent.asynchttp
import confluent.selfservice as selfservice
import confluent.shellserver as shellserver
import confluent.tlvdata
import confluent.util as util
import copy
import eventlet
import eventlet.greenthread
import greenlet
import json
import socket
import sys
import traceback
import time
try:
    import urlparse
except ModuleNotFoundError:
    import urllib.parse as urlparse
import eventlet.websocket
import eventlet.wsgi
#scgi = eventlet.import_patched('flup.server.scgi')
tlvdata = confluent.tlvdata


auditlog = None
tracelog = None
consolesessions = {}
confluent.asynchttp.set_console_sessions(consolesessions)
httpsessions = {}
opmap = {
    'POST': 'create',
    'GET': 'retrieve',
    'PUT': 'update',
    'DELETE': 'delete',
}


def group_creation_resources():
    yield confluent.messages.Attributes(
        kv={'name': None}, desc="Name of the group").html() + '<br>'
    yield confluent.messages.ListAttributes(kv={'nodes': []},
                                            desc='Nodes to add to the group'
                                            ).html() + '<br>\n'
    for attr in sorted(attribs.node):
        if attr == 'groups':
            continue
        if attr.startswith("secret."):
            yield confluent.messages.CryptedAttributes(
                kv={attr: None},
                desc=attribs.node[attr]['description']).html() + '<br>\n'
        elif ('type' in attribs.node[attr] and
                list == attribs.node[attr]['type']):
            yield confluent.messages.ListAttributes(
                kv={attr: []},
                desc=attribs.node[attr]['description']).html() + '<br>\n'
        else:
            yield confluent.messages.Attributes(
                kv={attr: None},
                desc=attribs.node[attr]['description']).html() + '<br>\n'


def node_creation_resources():
    yield confluent.messages.Attributes(
        kv={'name': None}, desc="Name of the node").html() + '<br>'
    for attr in sorted(attribs.node):
        if attr.startswith("secret."):
            yield confluent.messages.CryptedAttributes(
                kv={attr: None},
                desc=attribs.node[attr]['description']).html() + '<br>\n'
        elif ('type' in attribs.node[attr] and
                list == attribs.node[attr]['type']):
            yield confluent.messages.ListAttributes(
                kv={attr: []},
                desc=attribs.node[attr]['description']).html() + '<br>\n'
        else:
            yield confluent.messages.Attributes(
                kv={attr: None},
                desc=attribs.node[attr]['description']).html() + '<br>\n'


def user_creation_resources():
    credential = {
        'uid': {
            'description': (''),
        },
        'username': {
            'description': (''),
        },
        'password': {
            'description': (''),
        },
        'privilege_level': {
            'description': (''),
        },
    }
    for attr in sorted(credential):
        if attr == "password":
            yield confluent.messages.CryptedAttributes(
                kv={attr: None},
                desc=credential[attr]['description']).html() + '<br>\n'
        else:
            yield confluent.messages.Attributes(
                kv={attr: None},
                desc=credential[attr]['description']).html() + '<br>\n'


create_resource_functions = {
    'nodes': node_creation_resources,
    'groups': group_creation_resources,
    'users': user_creation_resources,
}


def _sessioncleaner():
    while True:
        currtime = time.time()
        targsessions = []
        for session in httpsessions:
            if httpsessions[session]['expiry'] < currtime:
                targsessions.append(session)
        for session in targsessions:
            forwarder.close_session(session)
            del httpsessions[session]
        targsessions = []
        for session in consolesessions:
            if consolesessions[session]['expiry'] < currtime:
                targsessions.append(session)
        for session in targsessions:
            del consolesessions[session]
        eventlet.sleep(10)


def _get_query_dict(env, reqbody, reqtype):
    qdict = {}
    try:
        qstring = env['QUERY_STRING']
    except KeyError:
        qstring = None
    if qstring:
        for qpair in qstring.split('&'):
            qkey, qvalue = qpair.split('=')
            qdict[qkey] = qvalue
    if reqbody is not None:
        if "application/x-www-form-urlencoded" in reqtype:
            if not isinstance(reqbody, str):
                reqbody = reqbody.decode('utf8')
            pbody = urlparse.parse_qs(reqbody, True)
            for ky in pbody:
                if len(pbody[ky]) > 1:  # e.g. REST explorer
                    na = [i for i in pbody[ky] if i != '']
                    qdict[ky] = na
                else:
                    qdict[ky] = pbody[ky][0]
        elif 'application/json' in reqtype:
            if not isinstance(reqbody, str):
                reqbody = reqbody.decode('utf8')
            pbody = json.loads(reqbody)
            for key in pbody:
                qdict[key] = pbody[key]
    if 'restexplorerhonorkey' in qdict:
        nqdict = {}
        for key in qdict:
            if key == 'restexplorerop':
                nqdict[key] = qdict['restexplorerop']
                continue
            if key in qdict['restexplorerhonorkey']:
                nqdict[key] = qdict[key]
        qdict = nqdict
    return qdict

def _should_skip_authlog(env):
    if ('/console/session' in env['PATH_INFO'] or
            '/shell/sessions/' in env['PATH_INFO']):
        # we should only log starting of a console
        return True
    if '/sessions/current/async' in env['PATH_INFO']:
        # this is effectively invisible
        return True
    if (env['REQUEST_METHOD'] == 'GET' and
            ('/sensors/' in env['PATH_INFO'] or
             '/health/' in env['PATH_INFO'] or
             '/power/state' in env['PATH_INFO'] or
             '/nodes/' == env['PATH_INFO'] or
             '/sessions/current/info' == env['PATH_INFO'] or
                 (env['PATH_INFO'].startswith('/noderange/') and
                  env['PATH_INFO'].endswith('/nodes/')))):
        # these are pretty innocuous, and noisy to log.
        return True
    return False


def _csrf_exempt(path):
    # first a get of info to get CSRF key, also '/forward/web' to enable
    # the popup ability to just forward
    return path == '/sessions/current/info' or path.endswith('/forward/web')


def _csrf_valid(env, session):
    # This could be simplified into a statement, but this is more readable
    # to have it broken out
    if env['REQUEST_METHOD'] == 'GET' and _csrf_exempt(env['PATH_INFO']):
        # Provide a web client a safe hook to request the CSRF token
        # This means that we consider GET of /sessions/current/info to be
        # a safe thing to inflict via CSRF, since CORS should prevent
        # hypothetical attacker from reading the data and it has no
        # side effects to speak of
        return True
    if 'csrftoken' not in session:
        # The client has not (yet) requested CSRF protection
        # so we return true
        if 'HTTP_CONFLUENTAUTHTOKEN' in env:
            # The client has requested CSRF countermeasures,
            # oblige the request and apply a new token to the
            # session
            session['csrftoken'] = util.randomstring(32)
        elif 'HTTP_REFERER' in env:
            # If there is a referrer, make sure it stays consistent
            # across the session.  A change in referer is a bad thing
            try:
                referer = env['HTTP_REFERER'].split('/')[2]
            except IndexError:
                return False
            if 'validreferer' not in session:
                session['validreferer'] = referer
            elif session['validreferer'] != referer:
                return False
        return True
    # The session has CSRF protection enabled, only mark valid if
    # the client has provided an auth token and that token matches the
    # value protecting the session
    return ('HTTP_CONFLUENTAUTHTOKEN' in env and
            env['HTTP_CONFLUENTAUTHTOKEN'] == session['csrftoken'])


def _authorize_request(env, operation):
    """Grant/Deny access based on data from wsgi env

    """
    authdata = None
    name = ''
    sessionid = None
    cookie = Cookie.SimpleCookie()
    element = env['PATH_INFO']
    if element.startswith('/sessions/current/'):
        element = None
    if 'HTTP_COOKIE' in env:
        cidx = (env['HTTP_COOKIE']).find('confluentsessionid=')
        if cidx >= 0:
            sessionid = env['HTTP_COOKIE'][cidx+19:cidx+51]
            sessid = sessionid
            sessid = sessionid
            if sessionid in httpsessions:
                if _csrf_valid(env, httpsessions[sessionid]):
                    if env['PATH_INFO'] == '/sessions/current/logout':
                        targets = []
                        for mythread in httpsessions[sessionid]['inflight']:
                            targets.append(mythread)
                        for mythread in targets:
                            eventlet.greenthread.kill(mythread)
                        forwarder.close_session(sessionid)
                        del httpsessions[sessionid]
                        return ('logout',)
                    httpsessions[sessionid]['expiry'] = time.time() + 90
                    name = httpsessions[sessionid]['name']
                    authdata = auth.authorize(
                        name, element=element, operation=operation,
                        skipuserobj=httpsessions[sessionid]['skipuserobject'])
    if (not authdata) and 'HTTP_AUTHORIZATION' in env:
        if env['PATH_INFO'] == '/sessions/current/logout':
            if 'HTTP_REFERER' in env:
                # note that this doesn't actually do harm
                # otherwise, but this way do not give appearance
                # of something having a side effect if it has the smell
                # of a CSRF
                return {'code': 401}
            return ('logout',)
        if env['HTTP_AUTHORIZATION'].startswith('MultiBasic '):
            name, passphrase = base64.b64decode(
                env['HTTP_AUTHORIZATION'].replace('MultiBasic ', '')).split(b':', 1)
            passphrase = json.loads(passphrase)
        else:
            name, passphrase = base64.b64decode(
                env['HTTP_AUTHORIZATION'].replace('Basic ', '')).split(b':', 1)
        try:
            authdata = auth.check_user_passphrase(name, passphrase, operation=operation, element=element)
        except Exception as e:
            if hasattr(e, 'prompts'):
                return {'code': 403, 'prompts': e.prompts}
            raise
        if authdata is False:
            return {'code': 403}
        elif not authdata:
            return {'code': 401}
        sessid = util.randomstring(32)
        while sessid in httpsessions:
            sessid = util.randomstring(32)
        httpsessions[sessid] = {'name': name, 'expiry': time.time() + 90,
                                'skipuserobject': authdata[4],
                                'inflight': set([])}
        if 'HTTP_CONFLUENTAUTHTOKEN' in env:
            httpsessions[sessid]['csrftoken'] = util.randomstring(32)
        cookie['confluentsessionid'] = util.stringify(sessid)
        cookie['confluentsessionid']['secure'] = 1
        cookie['confluentsessionid']['httponly'] = 1
        cookie['confluentsessionid']['path'] = '/'
    skiplog = _should_skip_authlog(env)
    if authdata:
        auditmsg = {
            'user': util.stringify(name),
            'operation': operation,
            'target': env['PATH_INFO'],
        }
        authinfo = {'code': 200,
                    'cookie': cookie,
                    'cfgmgr': authdata[1],
                    'username': authdata[2],
                    'userdata': authdata[0]}
        if authdata[3] is not None:
            auditmsg['tenant'] = authdata[3]
            authinfo['tenant'] = authdata[3]
        auditmsg['user'] = util.stringify(authdata[2])
        if sessid is not None:
            authinfo['sessionid'] = sessid
        if not skiplog:
            auditlog.log(auditmsg)
        if 'csrftoken' in httpsessions[sessid]:
            authinfo['authtoken'] = httpsessions[sessid]['csrftoken']
        httpsessions[sessid]['cfgmgr'] = authdata[1]
        return authinfo
    elif authdata is None:
        return {'code': 401}
    else:
        return {'code': 403}


def _pick_mimetype(env):
    """Detect the http indicated mime to send back.

    Note that as it gets into the ACCEPT header honoring, it only looks for
    application/json and else gives up and assumes html.  This is because
    browsers are very chaotic about ACCEPT HEADER.  It is assumed that
    XMLHttpRequest.setRequestHeader will be used by clever javascript
    if the '.json' scheme doesn't cut it.
    """
    if env['PATH_INFO'].endswith('.json'):
        return 'application/json; charset=utf-8', '.json'
    elif env['PATH_INFO'].endswith('.html'):
        return 'text/html', '.html'
    elif 'HTTP_ACCEPT' in env and 'application/json' in env['HTTP_ACCEPT']:
        return 'application/json; charset=utf-8', ''
    else:
        return 'text/html', ''


def _assign_consessionid(consolesession):
    sessid = util.randomstring(32)
    while sessid in consolesessions:
        sessid = util.randomstring(32)
    consolesessions[sessid] = {'session': consolesession,
                               'expiry': time.time() + 60}
    return sessid

@eventlet.websocket.WebSocketWSGI.configured(
    supported_protocols=['confluent.console'])
def wsock_handler(ws):
    sessid = ws.wait()
    sessid = sessid.replace('ConfluentSessionId:', '')
    sessid = sessid[:-1]
    currsess = httpsessions.get(sessid, None)
    if not currsess:
        return
    authtoken = ws.wait()
    authtoken = authtoken.replace('ConfluentAuthToken:', '')
    authtoken = authtoken[:-1]
    if currsess['csrftoken'] != authtoken:
        return
    mythreadid = greenlet.getcurrent()
    httpsessions[sessid]['inflight'].add(mythreadid)
    name = httpsessions[sessid]['name']
    authdata = auth.authorize(name, ws.path)
    if not authdata:
        return
    if '/console/session' in ws.path or '/shell/sessions/' in ws.path:
        #hard bake JSON into this path, do not support other incarnations
        if '/console/session' in ws.path:
            prefix, _, _ = ws.path.partition('/console/session')
            shellsession = False
        elif '/shell/sessions/' in ws.path:
            prefix, _, _ = ws.path.partition('/shell/sessions')
            shellsession = True
        _, _, nodename = prefix.rpartition('/')
        geom = ws.wait()
        geom = geom[1:]
        geom = json.loads(geom)
        width = geom['width']
        height = geom['height']
        skipreplay = geom.get('skipreplay', False)
        cfgmgr = httpsessions[sessid]['cfgmgr']
        username = httpsessions[sessid]['name']
        def datacallback(data):
            if isinstance(data, dict):
                data = json.dumps(data)
                ws.send(u'!' + data)
            else:
                try:
                    data = data.decode('utf8')
                except UnicodeDecodeError:
                    data = data.decode('cp437')
                ws.send(u' ' + data)
        try:
            if shellsession:
                consession = shellserver.ShellSession(
                    node=nodename, configmanager=cfgmgr,
                    username=username, skipreplay=skipreplay,
                    datacallback=datacallback, width=width, height=height
                )
            else:
                consession = consoleserver.ConsoleSession(
                    node=nodename, configmanager=cfgmgr,
                    username=username, skipreplay=skipreplay,
                    datacallback=datacallback, width=width, height=height
                )
        except exc.NotFoundException:
            return
        clientmsg = ws.wait()
        try:
            while clientmsg is not None:
                if clientmsg[0] == ' ':
                    consession.write(clientmsg[1:])
                elif clientmsg[0] == '!':
                    cmd = json.loads(clientmsg[1:])
                    action = cmd.get('action', None)
                    if action == 'break':
                        consession.send_break()
                    elif action == 'resize':
                        consession.resize(
                            width=cmd['width'], height=cmd['height'])
                elif clientmsg[0] == '?':
                    ws.send(u'?')
                clientmsg = ws.wait()
        finally:
            consession.destroy()


def resourcehandler(env, start_response):
    try:
        if 'HTTP_SEC_WEBSOCKET_VERSION' in env:
            for rsp in wsock_handler(env, start_response):
                yield rsp
        for rsp in resourcehandler_backend(env, start_response):
            yield rsp
    except Exception as e:
        tracelog.log(traceback.format_exc(), ltype=log.DataTypes.event,
                     event=log.Events.stacktrace)
        start_response('500 - ' + str(e), [])
        yield '500 - ' + str(e)
        return


def resourcehandler_backend(env, start_response):
    """Function to handle new wsgi requests
    """
    mimetype, extension = _pick_mimetype(env)
    headers = [('Content-Type', mimetype), ('Cache-Control', 'no-store'),
               ('Pragma', 'no-cache'),
               ('X-Content-Type-Options', 'nosniff'),
               ('Content-Security-Policy', "default-src 'self'"),
               ('X-XSS-Protection', '1; mode=block'), ('X-Frame-Options', 'deny'),
               ('Strict-Transport-Security', 'max-age=86400'),
               ('X-Permitted-Cross-Domain-Policies', 'none')]
    reqbody = None
    reqtype = None
    if env.get('PATH_INFO', '').startswith('/self/'):
        for res in selfservice.handle_request(env, start_response):
            yield res
        return
    if 'CONTENT_LENGTH' in env and int(env['CONTENT_LENGTH']) > 0:
        reqbody = env['wsgi.input'].read(int(env['CONTENT_LENGTH']))
        reqtype = env['CONTENT_TYPE']
    operation = opmap[env['REQUEST_METHOD']]
    querydict = _get_query_dict(env, reqbody, reqtype)
    if operation != 'retrieve' and 'restexplorerop' in querydict:
        operation = querydict['restexplorerop']
        del querydict['restexplorerop']
    authorized = _authorize_request(env, operation)
    if 'logout' in authorized:
        start_response('200 Successful logout', headers)
        yield('{"result": "200 - Successful logout"}')
        return
    if 'HTTP_SUPPRESSAUTHHEADER' in env or 'HTTP_CONFLUENTAUTHTOKEN' in env:
        badauth = [('Content-type', 'text/plain')]
    else:
        badauth = [('Content-type', 'text/plain'),
                   ('WWW-Authenticate', 'Basic realm="confluent"')]
    if authorized['code'] == 401:
        start_response('401 Authentication Required', badauth)
        yield 'authentication required'
        return
    if authorized['code'] == 403:
        start_response('403 Forbidden', badauth)
        response = {'result': 'Forbidden'}
        if 'prompts' in authorized:
            response['prompts'] = []
            for prompt in authorized['prompts']:
                if not isinstance(prompt, str):
                    prompt = prompt.decode('utf8')
                response['prompts'].append(prompt)
        yield json.dumps(response)
        return
    if authorized['code'] != 200:
        raise Exception("Unrecognized code from auth engine")
    headers.extend(
        ("Set-Cookie", m.OutputString())
        for m in authorized['cookie'].values())
    cfgmgr = authorized['cfgmgr']
    if (operation == 'create') and env['PATH_INFO'] == '/sessions/current/async':
        pagecontent = ""
        try:
            for rsp in _assemble_json(
                    confluent.asynchttp.handle_async(
                            env, querydict,
                            httpsessions[authorized['sessionid']]['inflight'])):
                pagecontent += rsp
            start_response("200 OK", headers)
            if not isinstance(pagecontent, bytes):
                pagecontent = pagecontent.encode('utf-8')
            yield pagecontent
            return
        except exc.ConfluentException as e:
            if e.apierrorcode == 500:
                # raise generics to trigger the tracelog
                raise
            start_response('{0} {1}'.format(e.apierrorcode, e.apierrorstr),
                           headers)
            yield e.get_error_body()
    elif (env['PATH_INFO'].endswith('/forward/web') and
              env['PATH_INFO'].startswith('/nodes/')):
        prefix, _, _ = env['PATH_INFO'].partition('/forward/web')
        _, _, nodename = prefix.rpartition('/')
        hm = cfgmgr.get_node_attributes(nodename, 'hardwaremanagement.manager')
        targip = hm.get(nodename, {}).get(
            'hardwaremanagement.manager', {}).get('value', None)
        if not targip:
            start_response('404 Not Found', headers)
            yield 'No hardwaremanagement.manager defined for node'
            return
        funport = forwarder.get_port(targip, env['HTTP_X_FORWARDED_FOR'],
                                     authorized['sessionid'])
        host = env['HTTP_X_FORWARDED_HOST']
        if ']' in host:
            host = host.split(']')[0] + ']'
        elif ':' in host:
            host = host.rsplit(':', 1)[0]
        url = 'https://{0}:{1}/'.format(host, funport)
        start_response('302', [('Location', url)])
        yield 'Our princess is in another castle!'
        return
    elif (operation == 'create' and ('/console/session' in env['PATH_INFO'] or
            '/shell/sessions/' in env['PATH_INFO'])):
        #hard bake JSON into this path, do not support other incarnations
        if '/console/session' in env['PATH_INFO']:
            prefix, _, _ = env['PATH_INFO'].partition('/console/session')
            shellsession = False
        elif '/shell/sessions/' in env['PATH_INFO']:
            prefix, _, _ = env['PATH_INFO'].partition('/shell/sessions')
            shellsession = True
        _, _, nodename = prefix.rpartition('/')
        if 'session' not in querydict.keys() or not querydict['session']:
            auditmsg = {
                'operation': 'start',
                'target': env['PATH_INFO'],
                'user': util.stringify(authorized['username']),
            }
            if 'tenant' in authorized:
                auditmsg['tenant'] = authorized['tenant']
            auditlog.log(auditmsg)
            # Request for new session
            skipreplay = False
            if 'skipreplay' in querydict and querydict['skipreplay']:
                skipreplay = True
            width = querydict.get('width', 80)
            height = querydict.get('height', 24)
            datacallback = None
            asynchdl = None
            if 'HTTP_CONFLUENTASYNCID' in env:
                asynchdl = confluent.asynchttp.get_async(env, querydict)
                termrel = asynchdl.set_term_relation(env)
                datacallback = termrel.got_data
            try:
                if shellsession:
                    consession = shellserver.ShellSession(
                        node=nodename, configmanager=cfgmgr,
                        username=authorized['username'], skipreplay=skipreplay,
                        datacallback=datacallback, width=width, height=height
                    )
                else:
                    consession = consoleserver.ConsoleSession(
                        node=nodename, configmanager=cfgmgr,
                        username=authorized['username'], skipreplay=skipreplay,
                        datacallback=datacallback, width=width, height=height
                    )
            except exc.NotFoundException:
                start_response("404 Not found", headers)
                yield "404 - Request Path not recognized"
                return
            if not consession:
                start_response("500 Internal Server Error", headers)
                return
            sessid = _assign_consessionid(consession)
            if asynchdl:
                asynchdl.add_console_session(sessid)
            start_response('200 OK', headers)
            yield '{"session":"%s","data":""}' % sessid
            return
        elif 'bytes' in querydict.keys():  # not keycodes...
            myinput = querydict['bytes']
            sessid = querydict['session']
            if sessid not in consolesessions:
                start_response('400 Expired Session', headers)
                return
            consolesessions[sessid]['expiry'] = time.time() + 90
            consolesessions[sessid]['session'].write(myinput)
            start_response('200 OK', headers)
            yield json.dumps({'session': querydict['session']})
            return  # client has requests to send or receive, not both...
        elif 'closesession' in querydict:
            consolesessions[querydict['session']]['session'].destroy()
            del consolesessions[querydict['session']]
            start_response('200 OK', headers)
            yield '{"sessionclosed": true}'
            return
        elif 'action' in querydict:
            if querydict['action'] == 'break':
                consolesessions[querydict['session']]['session'].send_break()
            elif querydict['action'] == 'resize':
                consolesessions[querydict['session']]['session'].resize(
                    width=querydict['width'], height=querydict['height'])
            elif querydict['action'] == 'reopen':
                consolesessions[querydict['session']]['session'].reopen()
            else:
                start_response('400 Bad Request')
                yield 'Unrecognized action ' + querydict['action']
                return
            start_response('200 OK', headers)
            yield json.dumps({'session': querydict['session']})
        else:  # no keys, but a session, means it's hooking to receive data
            sessid = querydict['session']
            if sessid not in consolesessions:
                start_response('400 Expired Session', headers)
                yield ''
                return
            consolesessions[sessid]['expiry'] = time.time() + 90
            # add our thread to the 'inflight' to have a hook to terminate
            # a long polling request
            loggedout = None
            mythreadid = greenlet.getcurrent()
            httpsessions[authorized['sessionid']]['inflight'].add(mythreadid)
            try:
                outdata = consolesessions[sessid]['session'].get_next_output(
                    timeout=25)
            except greenlet.GreenletExit as ge:
                loggedout = ge
            httpsessions[authorized['sessionid']]['inflight'].discard(
                    mythreadid)
            if sessid not in consolesessions:
                start_response('400 Expired Session', headers)
                yield ''
                return
            if loggedout is not None:
                consolesessions[sessid]['session'].destroy()
                start_response('401 Logged out', headers)
                yield '{"loggedout": 1}'
                return
            bufferage = False
            if 'stampsent' not in consolesessions[sessid]:
                consolesessions[sessid]['stampsent'] = True
                bufferage = consolesessions[sessid]['session'].get_buffer_age()
            if isinstance(outdata, dict):
                rspdata = outdata
                rspdata['session'] = querydict['session']
            else:
                rspdata = {'session': querydict['session'],
                           'data': outdata}
            if bufferage is not False:
                rspdata['bufferage'] = bufferage
            try:
                rsp = json.dumps(rspdata)
            except UnicodeDecodeError:
                try:
                    rsp = json.dumps(rspdata, encoding='cp437')
                except UnicodeDecodeError:
                    rsp = json.dumps({'session': querydict['session'],
                                      'data': 'DECODEERROR'})
            start_response('200 OK', headers)
            yield rsp
            return
    else:
        # normal request
        url = env['PATH_INFO']
        url = url.replace('.json', '')
        url = url.replace('.html', '')
        if url == '/sessions/current/info':
            start_response('200 OK', headers)
            sessinfo = {'username': authorized['username']}
            if 'authtoken' in authorized:
                sessinfo['authtoken'] = authorized['authtoken']
            if 'sessionid' in authorized:
                sessinfo['sessionid'] = authorized['sessionid']
            tlvdata.unicode_dictvalues(sessinfo)
            yield json.dumps(sessinfo)
            return
        resource = '.' + url[url.rindex('/'):]
        lquerydict = copy.deepcopy(querydict)
        try:
            hdlr = pluginapi.handle_path(url, operation,
                                         cfgmgr, querydict)
            if 'HTTP_CONFLUENTASYNCID' in env:
                confluent.asynchttp.run_handler(hdlr, env)
                start_response('202 Accepted', headers)
                yield 'Request queued'
                return
            pagecontent = ""
            if mimetype == 'text/html':
                for datum in _assemble_html(hdlr, resource, lquerydict, url,
                                            extension):
                    pagecontent += datum
            else:
                for datum in _assemble_json(hdlr, resource, url, extension):
                    pagecontent += datum
            start_response('200 OK', headers)
            if not isinstance(pagecontent, bytes):
                pagecontent = pagecontent.encode('utf-8')
            yield pagecontent
        except exc.ConfluentException as e:
            if ((not isinstance(e, exc.LockedCredentials)) and
                    e.apierrorcode == 500):
                # raise generics to trigger the tracelog
                raise
            start_response('{0} {1}'.format(e.apierrorcode, e.apierrorstr),
                           headers)
            yield e.get_error_body()

def _assemble_html(responses, resource, querydict, url, extension):
    yield '<html><head><meta charset="UTF-8"><title>' \
          'Confluent REST Explorer: ' + url + '</title></head>' \
                                              '<body><form action="' + \
                                              resource + '" method="post">'
    if querydict:
        yield 'Response to input data:<br>' + \
              json.dumps(querydict, separators=(',', ': '),
                         indent=4, sort_keys=True) + '<hr>'
    yield 'Only fields that have their boxes checked will have their ' \
          'respective values honored by the confluent server.<hr>' \
          '<input type="hidden" name="restexplorerhonorkey" value="">' + \
          '<a rel="self" href="{0}{1}">{0}{1}</a><br>'.format(
              resource, extension)
    if url == '/':
        iscollection = True
    elif resource[-1] == '/':
        iscollection = True
        yield '<a rel="collection" href="../{0}">../{0}</a><br>'.format(
            extension)
    else:
        iscollection = False
        yield '<a rel="collection" href="./{0}">./{0}</a><br>'.format(
            extension)
    pendingrsp = []
    for rsp in responses:
        if isinstance(rsp, confluent.messages.LinkRelation):
            yield rsp.html(extension) + "<br>"
        else:
            pendingrsp.append(rsp)
    for rsp in pendingrsp:
        yield rsp.html() + "<br>"
    if iscollection:
        # localpath = url[:-2] (why was this here??)
        try:
            if url == '/users/':
                return
            firstpass = True
            module = url.split('/')
            if not module:
                return
            for y in create_resource_functions[module[-2]]():
                if firstpass:
                    yield "<hr>Define new resource in %s:<BR>" % module[-2]
                firstpass = False
                yield y
            yield ('<input value="create" name="restexplorerop" type="submit">'
                   '</form></body></html>')
        except KeyError:
            pass
    else:
        yield ('<input value="update" name="restexplorerop" type="submit">'
               '</form></body></html>')


def _assemble_json(responses, resource=None, url=None, extension=None):
    #NOTE(jbjohnso) I'm considering giving up on yielding bit by bit
    #in json case over http.  Notably, duplicate key values from plugin
    #overwrite, but we'd want to preserve them into an array instead.
    #the downside is that http would just always blurt it ll out at
    #once and hold on to all the data in memory
    links = {}
    if resource is not None:
        links['self'] = {"href": resource + extension}
        if url == '/':
            pass
        elif resource[-1] == '/':
            links['collection'] = {"href": "../" + extension}
        else:
            links['collection'] = {"href": "./" + extension}
    rspdata = {}
    for rsp in responses:
        if isinstance(rsp, confluent.messages.LinkRelation):
            haldata = rsp.raw()
            for hk in haldata:
                if 'href' in haldata[hk]:
                    if isinstance(haldata[hk]['href'], int):
                        haldata[hk]['href'] = str(haldata[hk]['href'])
                    haldata[hk]['href'] += extension
                if hk in links:
                    if isinstance(links[hk], list):
                        links[hk].append(haldata[hk])
                    else:
                        links[hk] = [links[hk], haldata[hk]]
                elif hk == 'item':
                    links[hk] = [haldata[hk],]
                else:
                    links[hk] = haldata[hk]
        else:
            rsp = rsp.raw()
            for dk in rsp:
                if dk in rspdata:
                    if isinstance(rspdata[dk], list):
                        if isinstance(rsp[dk], list):
                            rspdata[dk].extend(rsp[dk])
                        else:
                            rspdata[dk].append(rsp[dk])
                    else:
                        rspdata[dk] = [rspdata[dk], rsp[dk]]
                else:
                    if dk == 'databynode' or dk == 'asyncresponse':
                        # a quirk, databynode suggests noderange
                        # multi response.  This should *always* be a list,
                        # even if it will be length 1
                        rspdata[dk] = [rsp[dk]]
                    else:
                        rspdata[dk] = rsp[dk]
    rspdata["_links"] = links
    tlvdata.unicode_dictvalues(rspdata)
    yield util.stringify(json.dumps(
        rspdata, sort_keys=True, indent=4, ensure_ascii=False).encode('utf-8'))


def serve(bind_host, bind_port):
    # TODO(jbjohnso): move to unix socket and explore
    # either making apache deal with it
    # or just supporting nginx or lighthttpd
    # for now, http port access
    #scgi.WSGIServer(resourcehandler, bindAddress=("localhost",4004)).run()
    #based on a bakeoff perf wise, eventlet http support proxied actually did
    #edge out patched flup.  unpatched flup was about the same as eventlet http
    #but deps are simpler without flup
    #also, the potential for direct http can be handy
    #todo remains unix domain socket for even http
    sock = None
    while not sock:
        try:
            sock = eventlet.listen(
                (bind_host, bind_port, 0, 0), family=socket.AF_INET6)
        except socket.error as e:
            if e.errno != 98:
                raise
            sys.stderr.write(
                'Failed to open HTTP due to busy port, trying again in'
                ' a second\n')
            eventlet.sleep(1)
    # TCP_FASTOPEN
    try:
        sock.setsockopt(socket.SOL_TCP, 23, 5)
    except Exception:
        pass  # we gave it our best shot there
    try:
        eventlet.wsgi.server(sock, resourcehandler, log=False, log_output=False,
                             debug=False, socket_timeout=60)
    except TypeError:
        # Older eventlet in place, skip arguments it does not understand
        eventlet.wsgi.server(sock, resourcehandler, log=False, debug=False)


class HttpApi(object):
    def __init__(self, bind_host=None, bind_port=None):
        self.server = None
        self.bind_host = bind_host or '::'
        self.bind_port = bind_port or 4005

    def start(self):
        global auditlog
        global tracelog
        tracelog = log.Logger('trace')
        auditlog = log.Logger('audit')
        self.server = eventlet.spawn(serve, self.bind_host, self.bind_port)


_cleaner = eventlet.spawn(_sessioncleaner)
