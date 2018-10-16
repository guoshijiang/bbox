import re
import time
import logging
import os, json
import asyncio
import json
from aiohttp import web
from functools import wraps
from aiobbox import testing
from aiobbox.jsonrpc import Request
from aiobbox.cluster import get_box, get_cluster
from aiobbox.exceptions import ServiceError, DataError
from aiobbox.utils import get_ssl_context, localbox_ip
from aiobbox.metrics import collect_metrics
from aiobbox import stats

DEBUG = True
srv_dict = {}

logger = logging.getLogger('bbox')

def has_service(srv):
    return srv in srv_dict

def srv_names(self):
    return list(srv_dict.keys())

class MethodRef:
    def __init__(self, fn, **kw):
        self.fn = fn

    def get_doc(self):
        return self.fn.__doc__ or ''

class Service(object):
    def __init__(self):
        self.methods = {}
        self.srv_name = None

    def register(self, srv_name):
        self.srv_name = srv_name
        if srv_name in srv_dict:
            logger.warn('srv {} already exist'.format(srv_name))
        srv_dict[srv_name] = self

    def method(self, name, for_test=False):
        def decorator(fn):
            if for_test and not testing.test_mode:
                # this method cannot be added
                # for non testing env
                return fn
            __w = wraps(fn)(fn)
            if name in self.methods:
                logger.warn('method {} already exist'.format(name))
            self.methods[name] = MethodRef(__w)
            return __w
        return decorator

    def get_docs(self, srv_name):
        arr = []
        for name, mref in sorted(self.methods.items()):
            doc = mref.get_doc()
            arr.append({
                'doc': doc,
                'name': name
                })
        return {
            'name': srv_name,
            'doc': self.__doc__,
            'methods': arr
            }

class ServiceRequest:
    srv = None
    req = None

    @classmethod
    def from_req(self, req):
        assert isinstance(req, Request)
        self.req = req

    def __init__(self, body):
        self.body = body

    async def handle(self):
        stats_name = None
        try:
            if self.req is None:
                self.req = Request(self.body)
            self.srv = srv_dict.get(self.req.srv_name)
            if not self.srv:
                raise ServiceError(
                    'service not found',
                    'server {} not found'.format(self.req.srv_name))

            if self.req.method == '__doc__':
                docs = self.srv.get_docs(self.req.srv_name)
                resp = {
                    'jsonrpc': '2.0',
                    'id': self.req.req_id,
                    'result': docs
                    }
            else:
                try:
                    method_ref = self.srv.methods[self.req.method]
                except KeyError:
                    raise ServiceError(
                        'method not found',
                        'Method {} does not exist'.format(
                            self.req.method))
                resp = await self.call_method(method_ref, self.req.srv_name)
        except DataError as e:
            error_info = {
                'message': str(e),
                'code': 'request parse error'
            }
            logger.warn(
                'json rpc error on parsing %s',
                self.req.body,
                exc_info=True)
            resp = {
                'jsonrpc': '2.0',
                'error': error_info,
                'id': self.req.body.get('id')
            }
        except ServiceError as e:
            error_info = {
                'message': getattr(e, 'message', str(e)),
                'code': e.code
            }
            logger.warn(
                'service error on JSON-RPC id %s',
                self.req.req_id,
                exc_info=True)
            resp = {
                'jsonrpc': '2.0',
                'error': error_info,
                'id': self.req.req_id}
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error('error on JSON-RPC id %s',
                          self.req.req_id,
                          exc_info=True)

            if stats_name:
                stats.error_rpc_request_count.incr(
                    stats_name)

            error_info = {
                'message': getattr(e, 'message', str(e)),
                }
            code = getattr(e, 'code', None)
            if code:
                error_info['code'] = code
            else:
                error_info['code'] = e.__class__.__name__
            if DEBUG:
                error_info['stack'] = traceback.format_exc()
            resp = {'error': error_info,
                    'id': self.req.req_id,
                    'jsonrpc': '2.0'}
        return resp

    async def call_method(self, method_ref, srv_name):
            start_time = time.time()
            stats_name = '/{}/{}'.format(
                srv_name, self.req.method)
            stats.rpc_request_count.incr(stats_name)
            res = await method_ref.fn(self, *self.req.params)
            resp = {'result': res,
                    'id': self.req.req_id,
                    'jsonrpc': '2.0'}
            end_time = time.time()
            if end_time - start_time > 1.0:
                stats.slow_rpc_request_count.incr(stats_name)
            return resp

    async def handle_ws(self, ws):
        resp = await self.handle()
        if resp:
            await ws.send_json(resp)
        return resp

async def handle(request):
    body = await request.json()
    sreq = ServiceRequest(body)
    resp = await sreq.handle()
    return web.json_response(resp)

async def handle_ws(request):
    ws = web.WebSocketResponse(autoping=True)
    await ws.prepare(request)

    async for req_msg in ws:
        body = json.loads(req_msg.data)
        req = ServiceRequest(body)
        asyncio.ensure_future(req.handle_ws(ws))

async def index(request):
    return web.Response(text='hello')

async def handle_metrics_json(request):
    resp = await collect_metrics()
    box = get_box()
    for name, labels, v in resp['lines']:
        labels['box'] = box.boxid
    return web.json_response(resp)

async def handle_metrics(request):
    '''
    aggregate metrics of all nodes
    '''
    resp = await collect_metrics()
    lines = []

    for name, define in resp['meta'].items():
        lines.append('# HELP {} {}'.format(
            name, define['help']))
        lines.append('# TYPE {} {}'.format(
            name, define['type']))
    for name, labels, v in resp['lines']:
        d = ', '.join('{}="{}"'.format(lname, lvalue)
                      for lname, lvalue in labels.items())
        d = '{' + d + '}'
        lines.append('{} {} {}'.format(name, d, v))
    return web.Response(text='\n'.join(lines))

async def start_server(args):
    boxid = args.boxid

    ssl_context = get_ssl_context(args.ssl)

    # server etcd agent
    srv_names = list(srv_dict.keys())
    curr_box = get_box()
    curr_box.ssl_prefix = args.ssl
    await curr_box.start(boxid, srv_names)

    app = web.Application()
    app.router.add_post('/jsonrpc/2.0/api', handle)
    app.router.add_route('*', '/jsonrpc/2.0/ws', handle_ws)
    app.router.add_get('/metrics', handle_metrics)
    app.router.add_get('/metrics.json', handle_metrics_json)
    app.router.add_get('/', index)

    host, port = curr_box.bind.split(':')
    if not localbox_ip(host):
        host = '0.0.0.0'
    logger.warn('box {} launched as {}'.format(
        curr_box.boxid,
        curr_box.bind))
    handler = app.make_handler()
    loop = asyncio.get_event_loop()
    srv = await loop.create_server(handler,
                                   host, port,
                                   ssl=ssl_context)
    return srv, handler
