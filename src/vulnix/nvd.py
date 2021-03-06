from .utils import batch
from BTrees import OOBTree
from lxml import etree
from persistent import Persistent
import ZODB
import ZODB.FileStorage
import datetime
import gzip
import logging
import os
import os.path
import requests
import shutil
import tempfile
import time
import transaction

DEFAULT_MIRROR = 'https://static.nvd.nist.gov/feeds/xml/cve/'
DEFAULT_CACHE_DIR = '~/.cache/vulnix'

NS = {'feed': 'http://scap.nist.gov/schema/feed/vulnerability/2.0',
      'vuln': 'http://scap.nist.gov/schema/vulnerability/0.4'}

logger = logging.getLogger(__name__)


class NVD(object):
    """Access to the National Vulnerability Database.

    https://nvd.nist.gov/

    """

    has_updates = False

    def __init__(self, mirror=DEFAULT_MIRROR, cache_dir=DEFAULT_CACHE_DIR):
        self.mirror = mirror.rstrip('/') + '/'
        self.cache_dir = cache_dir

        current_year = datetime.datetime.today().year
        self.relevant_archives = [
            str(x) for x in range(current_year - 5, current_year + 1)]
        self.relevant_archives.append('Modified')

    def __enter__(self):
        storage = ZODB.FileStorage.FileStorage(self.cache_dir + '/' + 'Data.fs')
        self._db = ZODB.DB(storage)
        self._connection = self._db.open()
        self._root = self._connection.root()
        self._root.setdefault('meta', Meta())
        self._root.setdefault('archives', OOBTree.OOBTree())

    def __exit__(self, exc_type, exc_value, exc_tb):
        if exc_type is None:
            transaction.commit()
            meta = self._root['meta']
            if self.has_updates:
                meta.unpacked += 1
                if meta.unpacked > 25:
                    logger.debug('Packing database')
                    self._db.pack()
                    meta.unpacked = 0
                transaction.commit()
        else:
            transaction.abort()
        self._connection.close()

    def by_product_name(self, name):
        for archive in self._root['archives'].values():
            yield from archive.by_product_name(name)

    def update(self):
        # Add missing archives
        for a in self.relevant_archives:
            if a not in self._root['archives']:
                self._root['archives'][a] = Archive(a)

        # Remove superfluous archives
        for a in self._root['archives']:
            if a not in self.relevant_archives:
                del self._root['archives'][a]

        for archive in self._root['archives'].values():
            # Ensure proper frequency.
            if archive.name == 'Modified':
                # Is only updated every two hours. Check hourly.
                archive.age_limit = 60 * 60
            elif archive.name == str(datetime.datetime.today().year):
                # The current year is only updated every 8 days (folding in the
                # data from Modified), check once every day.
                archive.age_limit = 1 * 24 * 60 * 60
            else:
                archive.age_limit = None
            self.has_updates |= archive.update(self.mirror)
            logging.debug('{} has {} products'.format(
                archive.name, len(archive.products or [])))
        transaction.commit()


class Meta(Persistent):
    """A small grab bag to store persistent meta information in
    the database.

    """

    unpacked = 0


class Archive(Persistent):

    # Either set to a duration to update every `age_limit` seconds or to None
    # to never update after the initial fetch.
    age_limit = None
    name = None
    products = None
    last_update = 0

    _cleanup = ()

    def __init__(self, name):
        self.name = name

    @property
    def upstream_filename(self):
        return 'nvdcve-2.0-{}.xml.gz'.format(self.name)

    @property
    def is_current(self):
        if self.age_limit is None:
            # We don't want to update and we have been fetched before - we're
            # good.
            return bool(self.last_update)
        age = time.time() - self.last_update
        return age < self.age_limit

    def by_product_name(self, name):
        return self.products.get(name, [])

    def update(self, mirror):
        if self.is_current:
            logger.info('{} is up-to-date.'.format(self.name))
            return False
        # Delete the parsed data.
        self.products = OOBTree.OOBTree()
        logger.info('Updating {}'.format(self.name))
        try:
            filename = self.download(mirror)
            self.parse(filename)
        except Exception:
            self.clean()
            raise
        self.last_update = time.time()
        return True

    def download(self, mirror):
        self._cleanup = []

        # Phase 1: download
        _, compressed = tempfile.mkstemp()
        self._cleanup.append(compressed)

        url = mirror + self.upstream_filename
        logger.debug("Downloading {}".format(url))
        r = requests.get(url, stream=True)
        r.raise_for_status()
        with open(compressed, 'wb') as fd:
            for chunk in r.iter_content(128*1024):
                fd.write(chunk)

        # Phase 2: uncompress
        _, uncompressed = tempfile.mkstemp()
        self._cleanup.append(uncompressed)
        logger.debug("Uncompressing {}".format(compressed))
        with gzip.open(compressed, 'rb') as f_in:
            with open(uncompressed, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        return uncompressed

    def parse(self, filename):
        logger.debug("Parsing {}".format(filename))
        parser = etree.iterparse(
            filename, tag='{' + NS['feed'] + '}entry')
        for event, node in batch(parser, 500, transaction.savepoint):
            vx = Vulnerability.from_node(node)
            # We don't use a ZODB set here as we a) won't ever change this
            # again in the future (we just rebuild the tree) and also I want to
            # keep records more coherent to avoid making millions of
            # micro-records.
            for cpe in vx.affected_products:
                self.products.setdefault(cpe.product, set())
                self.products[cpe.product].add(vx)
            # We need to explicitly clear this node. iterparse only builds the
            # tree incrementally but does not remove data that isn't needed any
            # longer.  See
            # http://www.ibm.com/developerworks/xml/library/x-hiperfparse/
            node.clear()
            while node.getprevious() is not None:
                del node.getparent()[0]

    def clean(self):
        for file in self._cleanup:
            if os.path.exists(file):
                os.unlink(file)


class Vulnerability(Persistent):

    cve_id = None
    affected_products = ()

    def __init__(self):
        self.affected_products = []

    @property
    def url(self):
        return ('https://web.nvd.nist.gov/view/vuln/detail?vulnId={}'.
                format(self.cve_id))

    @staticmethod
    def from_node(node):
        self = Vulnerability()
        self.cve_id = node.get('id')
        affected_products = {}
        for product in node.findall('.//vuln:product', NS):
            cpe = CPE.from_uri(product.text)
            if cpe.product is None:
                # This usually indicates a combination where some vulnerability
                # only applies for a specific operating system *vendor*
                continue
            key = (cpe.vendor, cpe.product)
            master_cpe = affected_products.setdefault(key, cpe)
            master_cpe.versions.update(cpe.versions)
        self.affected_products = list(affected_products.values())
        return self


class CPE(Persistent):

    # These are the only attributes we're interested in. Reduce memory
    # footprint by not storing unused attributes.

    vendor = None
    product = None

    @staticmethod
    def from_uri(uri):
        self = CPE()
        self.versions = set()
        protocol, identifier = uri.split(':/')
        assert protocol == 'cpe'
        component_list = identifier.split(':')
        components = ['part', 'vendor', 'product', 'version', 'update',
                      'edition', 'lang']
        while component_list:
            component_name = components.pop(0)
            component_value = component_list.pop(0)
            if component_name == 'version':
                self.versions.add(component_value)
            elif hasattr(self, component_name):
                setattr(self, component_name, component_value)
        return self

    def __repr__(self):
        return '<CPE %s>' % self.uri
