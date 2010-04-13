#! /usr/bin/env python

import os
import sys
import base64
import httplib
import urllib
import mimetypes
import datetime
from urlparse import urlparse
from getpass import getpass

try:
    import json
except ImportError:
    import simplejson as json

DEFAULT_CONFIG = {
    'api_server': 'https://api-dev.bugzilla.mozilla.org/latest',
    'server': 'https://bugzilla.mozilla.org'
}

def json_request(method, url, query_args=None, body=None):
    if query_args is None:
        query_args = {}

    headers = {'Accept': 'application/json',
               'Content-Type': 'application/json'}

    urlparts = urlparse(url)
    if urlparts.scheme == 'https':
        connclass = httplib.HTTPSConnection
    elif urlparts.scheme == 'http':
        connclass = httplib.HTTPConnection
    else:
        raise ValueError('unknown scheme "%s"' % urlparts.scheme)
    conn = connclass(urlparts.netloc)
    path = urlparts.path
    if query_args:
        path += '?%s' % urllib.urlencode(query_args)
    if body is not None:
        body = json.dumps(body)
    conn.request(method, path, body, headers)
    response = conn.getresponse()
    status, reason = response.status, response.reason
    mimetype = response.msg.gettype()
    data = response.read()
    conn.close()

    if mimetype == 'application/json':
        data = json.loads(data)

    return {'status': response.status,
            'reason': response.reason,
            'content_type': mimetype,
            'body': data}

def make_caching_json_request(cache, json_request=json_request):
    from hashlib import sha1 as hashfunc

    def caching_json_request(method, url, query_args=None, body=None):
        key = hashfunc(repr((method, url, query_args, body))).hexdigest()
        if not key in cache:
            cache[key] = json_request(method=method,
                                      url=url,
                                      query_args=query_args,
                                      body=body)
        return cache[key]

    return caching_json_request

class JsonBlobCache(object):
    def __init__(self, cachedir):
        self.cachedir = cachedir

    def __pathforkey(self, key):
        if not isinstance(key, basestring):
            raise ValueError('key must be a string')
        return os.path.join(self.cachedir, '%s.json' % key)

    def __getitem__(self, key):
        if not key in self:
            raise KeyError(key)
        return json.loads(open(self.__pathforkey(key)).read())

    def __setitem__(self, key, value):
        open(self.__pathforkey(key), 'w').write(json.dumps(value))

    def __contains__(self, key):
        return os.path.exists(self.__pathforkey(key))

def getpass_or_die(prompt, getpass=getpass):
    try:
        password = getpass(prompt)
    except KeyboardInterrupt:
        password = None

    if not password:
        print "Aborted."
        sys.exit(1)

    return password

def load_config(filename=None, getpass=None):
    config = {}
    config.update(DEFAULT_CONFIG)

    if not filename:
        filename = os.path.join('~', '.bugzilla-config.json')
        filename = os.path.expanduser(filename)
        if not os.path.exists(filename):
            return config

    config.update(json.loads(open(filename).read()))

    if getpass and 'username' in config and 'password' not in config:
        config['password'] = getpass('Enter password for %s: ' %
                                     config['username'])
    return config

class BugzillaApi(object):
    def __init__(self, config=None, jsonreq=None,
                 getpass=getpass_or_die):
        if config is None:
            config = load_config()

        if jsonreq is None:
            if 'cache_dir' in config:
                cache = JsonBlobCache(os.path.expanduser(config['cache_dir']))
                jsonreq = make_caching_json_request(cache)
            else:
                jsonreq = json_request

        self.config = config
        self.__jsonreq = jsonreq

    def post_attachment(self, bug_id, contents, filename, description,
                        content_type=None, is_patch=False, is_private=False,
                        is_obsolete=False,
                        guess_mime_type=mimetypes.guess_type):
        """
        >>> jsonreq = Mock('jsonreq')
        >>> jsonreq.mock_returns = {
        ...   "status": 201,
        ...   "body": {"ref": "http://foo/latest/attachment/1"},
        ...   "reason": "Created",
        ...   "content_type": "application/json"
        ... }
        >>> bzapi = BugzillaApi(config=TEST_CFG_WITH_LOGIN,
        ...                     jsonreq=jsonreq)
        >>> bzapi.post_attachment(bug_id=536619,
        ...                       contents="testing!",
        ...                       filename="contents.txt",
        ...                       description="test upload")
        Called jsonreq(
            body={'is_obsolete': False, 'flags': [],
                  'description': 'test upload',
                  'content_type': 'text/plain', 'encoding': 'base64',
                  'file_name': 'contents.txt', 'is_patch': False,
                  'data': 'dGVzdGluZyE=', 'is_private': False,
                  'size': 8},
            method='POST',
            query_args={'username': 'bar', 'password': 'baz'},
            url='http://foo/latest/bug/536619/attachment')
        {'ref': 'http://foo/latest/attachment/1'}
        """

        if content_type is None:
            content_type = guess_mime_type(filename)[0]
            if not content_type:
                raise ValueError('could not guess content type for "%s"' %
                                 filename)

        attachment = {
            'data': base64.b64encode(contents),
            'description': description,
            'encoding': 'base64',
            'file_name': filename,
            'flags': [],
            'is_obsolete': is_obsolete,
            'is_patch': is_patch,
            'is_private': is_private,
            'size': len(contents),
            'content_type': content_type
            }

        return self.request('POST', '/bug/%d/attachment' % bug_id,
                            body=attachment)

    def request(self, method, path, query_args=None, body=None):
        if query_args is None:
            query_args = {}

        if 'username' in self.config and 'password' in self.config:
            for name in ['username', 'password']:
                query_args[name] = self.config[name]

        url = '%s%s' % (self.config['api_server'], path)

        response = self.__jsonreq(method=method,
                                  url=url,
                                  query_args=query_args,
                                  body=body)

        if response['content_type'] == 'application/json':
            json_response = response['body']
            if 'error' in json_response and json_response['error']:
                raise BugzillaApiError(response)
            return json_response
        raise BugzillaApiError(response)

class BugzillaApiError(Exception):
    pass

def iso8601_to_datetime(timestamp):
    """
    >>> iso8601_to_datetime('2010-04-11T19:16:59Z')
    datetime.datetime(2010, 4, 11, 19, 16, 59)
    """

    return datetime.datetime.strptime(timestamp,
                                      "%Y-%m-%dT%H:%M:%SZ")

class Attachment(object):
    """
    >>> bzapi = Mock('bzapi')
    >>> bzapi.request.mock_returns = TEST_ATTACHMENT_WITH_DATA
    >>> a = Attachment(TEST_ATTACHMENT_WITHOUT_DATA, bzapi)
    >>> a.data
    Called bzapi.request(
        'GET',
        '/attachment/438797',
        query_args={'attachmentdata': '1'})
    'testing!'
    """

    def __init__(self, jsonobj, bzapi=None):
        self.bzapi = bzapi
        self.id = int(jsonobj['id'])
        self.description = jsonobj['description']
        self.content_type = jsonobj['content_type']
        for timeprop in ['last_change_time']:
            setattr(self, timeprop, jsonobj[timeprop])
        if 'data' in jsonobj:
            self.__data = self.__decode_data(jsonobj)
        else:
            self.__data = None

    @property
    def data(self):
        if self.__data is None and self.bzapi:
            jsonobj = self.__get_full_attachment(self.bzapi, self.id)
            self.__data = self.__decode_data(jsonobj)
        return self.__data

    def __decode_data(self, jsonobj):
        if jsonobj['encoding'] != 'base64':
            raise NotImplementedError("unrecognized encoding: %s" %
                                      jsonobj['encoding'])
        return base64.b64decode(jsonobj['data'])

    def __repr__(self):
        return '<Attachment %d - %s>' % (self.id, repr(self.description))

    @staticmethod
    def __get_full_attachment(bzapi, attach_id):
        return bzapi.request('GET', '/attachment/%d' % attach_id,
                             query_args={'attachmentdata': '1'})

    @classmethod
    def fetch(klass, bzapi, attach_id):
        """
        >>> bzapi = Mock('bzapi')
        >>> bzapi.request.mock_returns = TEST_ATTACHMENT_WITH_DATA
        >>> Attachment.fetch(bzapi, 438797)
        Called bzapi.request(
            'GET',
            '/attachment/438797',
            query_args={'attachmentdata': '1'})
        <Attachment 438797 - u'test upload'>
        """

        return klass(klass.__get_full_attachment(bzapi, attach_id))

class Bug(object):
    """
    >>> Bug(TEST_BUG)
    <Bug 558680 - u'Here is a summary'>
    """

    def __init__(self, jsonobj):
        self.id = int(jsonobj['id'])
        self.summary = jsonobj['summary']
        self.attachments = [Attachment(attach)
                            for attach in jsonobj['attachments']]

    def __repr__(self):
        return '<Bug %d - %s>' % (self.id, repr(self.summary))

    @classmethod
    def fetch(klass, bzapi, bug_id):
        """
        >>> bzapi = Mock('bzapi')
        >>> bzapi.request.mock_returns = TEST_BUG
        >>> Bug.fetch(bzapi, 558680)
        Called bzapi.request('GET', '/bug/558680')
        <Bug 558680 - u'Here is a summary'>
        """

        return klass(bzapi.request('GET', '/bug/%d' % bug_id))

if __name__ == '__main__':
    bzapi = BugzillaApi()
    print bzapi.request('GET', '/attachment/436897',
                        query_args={'attachmentdata': 1})
    #bzapi.post_attachment(bug_id=536619,
    #                      contents="testing!",
    #                      filename="contents.txt",
    #                      description="test upload")
