import asyncpg
import configparser
import json
import asyncio

from aiohttp import web
from contextlib import suppress
from abc import ABCMeta, abstractmethod
from aiohttp_security import permits
from aiohttp_security.api import AUTZ_KEY

from pyvospace.core.exception import *
from pyvospace.core.model import *

from .view import *
from .uws import UWSJobPool
from .database import NodeDatabase


class AbstractSpace(metaclass=ABCMeta):
    @abstractmethod
    def get_protocols(self) -> Protocols:
        raise NotImplementedError()

    @abstractmethod
    def get_views(self) -> Views:
        raise NotImplementedError()

    @abstractmethod
    def get_accept_views(self, node: Node):
        raise NotImplementedError()

    @abstractmethod
    def get_provide_views(self, node: Node):
        raise NotImplementedError()

    @abstractmethod
    async def move_storage_node(self, src: Node, dest: Node):
        raise NotImplementedError()

    @abstractmethod
    async def copy_storage_node(self, src: Node, dest: Node):
        raise NotImplementedError()

    @abstractmethod
    async def create_storage_node(self, node: Node):
        raise NotImplementedError()

    @abstractmethod
    async def delete_storage_node(self, node: Node):
        raise NotImplementedError()

    @abstractmethod
    async def set_protocol_transfer(self, job: UWSJob):
        raise NotImplementedError()


class SpaceServer(web.Application):
    def __init__(self, cfg_file, *args, **kwargs):
        super().__init__(*args, **kwargs)

        config = configparser.ConfigParser()
        config.read(cfg_file)
        self.config = config

        self.router.add_get('/vospace/protocols', self._get_protocols)
        self.router.add_get('/vospace/views', self._get_views)
        self.router.add_get('/vospace/nodes/{name:.*}', self._get_node)
        self.router.add_put('/vospace/nodes/{name:.*}', self._create_node)
        self.router.add_post('/vospace/nodes/{name:.*}', self._set_node_properties)
        self.router.add_delete('/vospace/nodes/{name:.*}', self._delete_node)
        self.router.add_post('/vospace/transfers', self._create_transfer)
        self.router.add_post('/vospace/synctrans', self._sync_transfer)
        self.router.add_get('/vospace/transfers/{job_id}', self._get_job)
        self.router.add_post('/vospace/transfers/{job_id}/phase', self._modify_job_phase)
        self.router.add_get('/vospace/transfers/{job_id}/phase', self._get_job_phase)
        self.router.add_get('/vospace/transfers/{job_id}/error', self._get_job)
        self.router.add_get('/vospace/transfers/{job_id}/results/transferDetails', self._get_transfer_details)
        self.on_shutdown.append(self.shutdown)

    async def setup(self, abstract_space):
        assert isinstance(abstract_space, AbstractSpace)
        self['abstract_space'] = abstract_space
        self['space_host'] = self.config['Space']['host']
        self['space_port'] = int(self.config['Space']['port'])
        self['space_name'] = self.config['Space']['name']
        self['uri'] = self.config['Space']['uri']
        self['parameters'] = json.loads(self.config['Space']['parameters'])
        db_pool = await asyncpg.create_pool(dsn=self.config['Space']['dsn'])
        space_id = await self._register_space(db_pool,
                                              self['space_name'],
                                              self['space_host'],
                                              self['space_port'],
                                              json.dumps(self['parameters']))

        self['db_pool'] = db_pool
        self['space_id'] = space_id
        self['executor'] = UWSJobPool(space_id, db_pool)
        self['db'] = NodeDatabase(space_id, db_pool, self)

    async def _register_space(self, db_pool, name, host, port, parameters):
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetchrow("select * from space where host=$1 and port=$2 for update",
                                             host, port)
                if result:
                    # if there is an existing plugin associated with this space
                    # and its not the one specified then raise an error
                    # Don't want to infringe on another space and its data
                    if result['name'] != name:
                        raise VOSpaceError(400, 'Can not start space over an existing '
                                                'space on the same host and port.')
                result = await conn.fetchrow("insert into space (host, port, name, parameters) "
                                             "values ($1, $2, $3, $4) on conflict (host, port) "
                                             "do update set parameters=$4 returning id",
                                             host, port, name, parameters)
                return int(result['id'])

    async def shutdown(self):
        await self['executor'].close()
        await self['db_pool'].close()

    async def permits(self, identity, permission, context):
        autz_policy = self.get(AUTZ_KEY)
        if autz_policy is None:
            return True
        return await autz_policy.permits(identity, permission, context)

    async def _get_protocols(self, request):
        try:
            protocols = self['abstract_space'].get_protocols()
            return web.Response(status=200, content_type='text/xml', text=protocols.tostring())

        except VOSpaceError as e:
            return web.Response(status=e.code, text=e.error)
        except Exception as g:
            return web.Response(status=500, text=str(g))

    async def _get_views(self, request):
        try:
            protocols = self['abstract_space'].get_views()
            return web.Response(status=200, content_type='text/xml', text=protocols.tostring())

        except VOSpaceError as e:
            return web.Response(status=e.code, text=e.error)
        except Exception as g:
            return web.Response(status=500, text=str(g))

    async def _set_node_properties(self, request):
        try:
            with suppress(asyncio.CancelledError):
                node = await asyncio.shield(set_node_properties_request(request))
            return web.Response(status=200, content_type='text/xml', text=node.tostring())

        except VOSpaceError as e:
            return web.Response(status=e.code, text=e.error)
        except Exception as g:
            return web.Response(status=500, text=str(g))

    async def _get_node(self, request):
        try:
            node = await get_node_request(request)
            return web.Response(status=200, content_type='text/xml', text=node.tostring())

        except VOSpaceError as e:
            return web.Response(status=e.code, text=e.error)
        except Exception as g:
            return web.Response(status=500, text=str(g))

    async def _create_node(self, request):
        try:
            with suppress(asyncio.CancelledError):
                node = await asyncio.shield(create_node_request(request))
            return web.Response(status=201, content_type='text/xml', text=node.tostring())

        except VOSpaceError as e:
            return web.Response(status=e.code, text=e.error)
        except Exception as g:
            return web.Response(status=500, text=str(g))

    async def _delete_node(self, request):
        try:
            with suppress(asyncio.CancelledError):
                await asyncio.shield(delete_node_request(self, request))
            return web.Response(status=204)

        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception as e:
            return web.Response(status=500)

    async def _sync_transfer(self, request):
        try:
            with suppress(asyncio.CancelledError):
                job = await asyncio.shield(sync_transfer_request(request))
            return web.HTTPSeeOther(location=f'/vospace/transfers/{job.job_id}'
                                             f'/results/transferDetails')
        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception as e:
            return web.Response(status=500)

    async def _create_transfer(self, request):
        try:
            with suppress(asyncio.CancelledError):
                job = await asyncio.shield(create_transfer_request(request))
            return web.HTTPSeeOther(location=f'/vospace/transfers/{job.job_id}')

        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception as e:
            return web.Response(status=500)

    async def _get_job(self, request):
        try:
            job = await get_job_request(request)
            return web.Response(status=200, content_type='text/xml', text=job.tostring())

        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception:
            return web.Response(status=500)

    async def _get_transfer_details(self, request):
        try:
            xml = await get_transfer_details_request(request)
            return web.Response(status=200, content_type='text/xml', text=xml)

        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception:
            return web.Response(status=500)

    async def _get_job_phase(self, request):
        try:
            phase_text = await get_job_phase_request(request)
            return web.Response(status=200, text=phase_text)

        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception:
            return web.Response(status=500)

    async def _modify_job_phase(self, request):
        try:
            with suppress(asyncio.CancelledError):
                job_id = await asyncio.shield(modify_job_request(request))
            return web.HTTPSeeOther(location=f'/vospace/transfers/{job_id}')

        except InvalidJobStateError:
            return web.HTTPSeeOther(location=f'/vospace/transfers/{job_id}')
        except VOSpaceError as f:
            return web.Response(status=f.code, text=f.error)
        except Exception:
            return web.Response(status=500)