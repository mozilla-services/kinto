from pyramid import httpexceptions
from pyramid.security import NO_PERMISSION_REQUIRED
from pyramid.view import view_config

from cliquet import resource
from cliquet.errors import http_error
from cliquet.utils import build_request, reapply_cors
from cliquet.storage import exceptions as storage_exceptions

from kinto.authorization import RouteFactory
from kinto.views import NameGenerator
from kinto.views.collections import Collection


@resource.register(name='bucket',
                   collection_methods=('GET', 'POST'),
                   collection_path='/buckets',
                   record_path='/buckets/{{id}}')
class Bucket(resource.ProtectedResource):
    permissions = ('read', 'write', 'collection:create', 'group:create')

    def __init__(self, *args, **kwargs):
        super(Bucket, self).__init__(*args, **kwargs)
        self.model.id_generator = NameGenerator()

    def get_parent_id(self, request):
        # Buckets are not isolated by user, unlike Cliquet resources.
        return ''

    def delete(self):
        result = super(Bucket, self).delete()

        # Delete groups.
        storage = self.model.storage
        parent_id = '/buckets/%s' % self.record_id
        storage.delete_all(collection_id='group',
                           parent_id=parent_id,
                           with_deleted=False)
        storage.purge_deleted(collection_id='group',
                              parent_id=parent_id)

        # Delete collections.
        deleted = storage.delete_all(collection_id='collection',
                                     parent_id=parent_id,
                                     with_deleted=False)
        storage.purge_deleted(collection_id='collection',
                              parent_id=parent_id)

        # Delete records.
        id_field = self.model.id_field
        for collection in deleted:
            parent_id = '/buckets/%s/collections/%s' % (self.record_id,
                                                        collection[id_field])
            storage.delete_all(collection_id='record',
                               parent_id=parent_id,
                               with_deleted=False)
            storage.purge_deleted(collection_id='record', parent_id=parent_id)

        return result


def create_bucket(request, bucket_id):
    """Create a bucket if it doesn't exists."""
    bucket_put = (request.method.lower() == 'put' and
                  request.path.endswith('buckets/default'))
    # Do nothing if current request will already create the bucket.
    if bucket_put:
        return

    # Do not intent to create multiple times per request (e.g. in batch).
    already_created = request.bound_data.setdefault('buckets', {})
    if bucket_id in already_created:
        return

    # Fake context to instantiate a Bucket resource.
    context = RouteFactory(request)
    context.get_permission_object_id = lambda r, i: '/buckets/%s' % bucket_id
    resource = Bucket(request, context)
    try:
        bucket = resource.model.create_record({'id': bucket_id})
    except storage_exceptions.UnicityError as e:
        bucket = e.record
    finally:
        already_created[bucket_id] = bucket


def create_collection(request, bucket_id):
    # Do nothing if current request does not involve a collection.
    subpath = request.matchdict.get('subpath')
    if not (subpath and subpath.startswith('collections/')):
        return

    collection_id = subpath.split('/')[1]
    collection_uri = '/buckets/%s/collections/%s' % (bucket_id, collection_id)

    # Do not intent to create multiple times per request (e.g. in batch).
    already_created = request.bound_data.setdefault('collections', {})
    if collection_uri in already_created:
        return

    # Do nothing if current request will already create the collection.
    collection_put = (request.method.lower() == 'put' and
                      request.path.endswith(collection_id))
    if collection_put:
        return

    # Fake context to instantiate a Collection resource.
    context = RouteFactory(request)
    context.get_permission_object_id = lambda r, i: collection_uri

    backup = request.matchdict
    request.matchdict = dict(bucket_id=bucket_id,
                             id=collection_id,
                             **request.matchdict)
    resource = Collection(request, context)
    try:
        collection = resource.model.create_record({'id': collection_id})
    except storage_exceptions.UnicityError as e:
        collection = e.record
    finally:
        already_created[collection_uri] = collection
    request.matchdict = backup


@view_config(route_name='default_bucket', permission=NO_PERMISSION_REQUIRED)
@view_config(route_name='default_bucket_collection',
             permission=NO_PERMISSION_REQUIRED)
def default_bucket(request):
    is_preflight_request = request.method.lower() == 'options'
    if is_preflight_request:
        path = request.path.replace('default', 'any_bucket')
        subrequest = build_request(request, {
            'method': 'OPTIONS',
            'path': path
        })
        return request.invoke_subrequest(subrequest)

    bucket_id = request.default_bucket_id

    if not is_preflight_request:
        # Only OPTIONS requests can be anonymous.
        if bucket_id is None:
            # Will pass through Cliquet @forbidden_view_config
            raise httpexceptions.HTTPForbidden()

        # Implicit bucket creation if does not exist
        create_bucket(request, bucket_id)

        # Implicit collection creation if does not exist
        create_collection(request, bucket_id)

    # Redirect the client to the actual bucket, using full URL
    path = request.path.replace('/buckets/default', '/buckets/%s' % bucket_id)
    querystring = request.url[(request.url.index(request.path) +
                               len(request.path)):]
    raise http_error(httpexceptions.HTTPTemporaryRedirect(path + querystring))
