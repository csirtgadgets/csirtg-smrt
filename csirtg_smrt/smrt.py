#!/usr/bin/env python

import logging
import os.path
import textwrap
from argparse import ArgumentParser
from argparse import RawDescriptionHelpFormatter
from random import randint
from time import sleep
from pprint import pprint
import traceback
import sys
import select

import csirtg_smrt.parser
from csirtg_smrt.archiver import Archiver
import csirtg_smrt.client
from csirtg_smrt.constants import REMOTE_ADDR, SMRT_RULES_PATH, SMRT_CACHE, CONFIG_PATH, RUNTIME_PATH, VERSION, FIREBALL_SIZE
from csirtg_smrt.rule import Rule
from csirtg_smrt.fetcher import Fetcher
from csirtg_smrt.utils import setup_logging, get_argument_parser, load_plugin, setup_signals, read_config, \
    setup_runtime_path
from csirtg_smrt.exceptions import AuthError, TimeoutError
from csirtg_indicator.format import FORMATS
from csirtg_indicator import Indicator

PARSER_DEFAULT = "pattern"
TOKEN = os.environ.get('CSIRTG_TOKEN', None)
TOKEN = os.environ.get('CSIRTG_SMRT_TOKEN', TOKEN)
ARCHIVE_PATH = os.environ.get('CSIRTG_SMRT_ARCHIVE_PATH', RUNTIME_PATH)
ARCHIVE_PATH = os.path.join(ARCHIVE_PATH, 'smrt.db')
FORMAT = os.environ.get('CSIRTG_SMRT_FORMAT', 'table')


# http://python-3-patterns-idioms-test.readthedocs.org/en/latest/Factory.html
# https://gist.github.com/pazdera/1099559
logging.getLogger("requests").setLevel(logging.WARNING)


class Smrt(object):
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def __enter__(self):
        return self

    def __init__(self, token=TOKEN, remote=REMOTE_ADDR, client='stdout', username=None, feed=None, archiver=None,
                 fireball=False, no_fetch=False, verify_ssl=True):

        self.logger = logging.getLogger(__name__)

        plugin_path = os.path.join(os.path.dirname(__file__), 'client')
        if getattr(sys, 'frozen', False):
            plugin_path = os.path.join(sys._MEIPASS, 'csirtg_smrt', 'client')

        self.client = load_plugin(plugin_path, client)

        if not self.client:
            raise RuntimeError("Unable to load plugin: {}".format(client))

        self.client = self.client(remote=remote, token=token, username=username, feed=feed, fireball=fireball,
                                  verify_ssl=verify_ssl)

        self.archiver = archiver
        self.fireball = fireball
        self.no_fetch = no_fetch

    def is_archived(self, indicator):
        if self.archiver and self.archiver.search(indicator):
            return True

    def archive(self, indicator):
        if self.archiver and self.archiver.create(indicator):
            return True

    def load_feeds(self, rule, feed=None):
        if isinstance(rule, str) and os.path.isdir(rule):
            for f in os.listdir(rule):
                if f.startswith('.'):
                    continue

                self.logger.debug("processing {0}/{1}".format(rule, f))
                r = Rule(path=os.path.join(rule, f))

                for feed in r.feeds:
                    yield r, feed

        else:
            self.logger.debug("processing {0}".format(rule))
            if isinstance(rule, str):
                rule = Rule(path=rule)

            if feed:
                # replace the feeds dict with the single feed
                # raises KeyError if it doesn't exist
                rule.feeds = {feed: rule.feeds[feed]}

            for f in rule.feeds:
                yield rule, f

    def load_parser(self, rule, feed, limit=None, data=None, filters=None):
        if isinstance(rule, str):
            rule = Rule(rule)

        fetch = Fetcher(rule, feed, data=data, no_fetch=self.no_fetch)

        parser_name = rule.parser or PARSER_DEFAULT
        plugin_path = os.path.join(os.path.dirname(__file__), 'parser')

        if getattr(sys, 'frozen', False):
            plugin_path = os.path.join(sys._MEIPASS, plugin_path)

        parser = load_plugin(plugin_path, parser_name)

        if parser is None:
            self.logger.info('trying z{}'.format(parser_name))
            parser = load_plugin(csirtg_smrt.parser.__path__[0], 'z{}'.format(parser_name))
            if parser is None:
                raise SystemError('Unable to load parser: {}'.format(parser_name))

        self.logger.debug("loading parser: {}".format(parser))

        return parser(self.client, fetch, rule, feed, limit=limit, archiver=self.archiver, filters=filters,
                      fireball=self.fireball)

    def process(self, rule, feed, limit=None, data=None, filters=None):
        parser = self.load_parser(rule, feed, limit=limit, data=data, filters=filters)

        self.archiver and self.archiver.begin()
        queue = []

        _limit = 0
        if limit:
            _limit = int(limit)

        for i in parser.process():
            if isinstance(i, dict):
                i = Indicator(**i)

            if not i.firsttime:
                i.firsttime = i.lasttime

            if not i.group:
                i.group = 'everyone'

            if self.is_archived(i):
                self.logger.debug('skipping: {}/{}/{}/{}'.format(i.indicator, i.provider, i.firsttime, i.lasttime))
            else:
                self.logger.debug('adding: {}/{}/{}/{}'.format(i.indicator, i.provider, i.firsttime, i.lasttime))
                if self.client != 'stdout':
                    if self.fireball:
                        queue.append(i)
                        if len(queue) == FIREBALL_SIZE:
                            self.logger.debug('flushing queue...')
                            self.client.indicators_create(queue)
                            queue = []
                    else:
                        self.client.indicators_create(i)
                yield i
                self.archive(i)

            if _limit:
                _limit -= 1

            if _limit == 0:
                self.logger.debug("limit reached...")
                break

        if self.fireball and len(queue) > 0:
            self.logger.debug('flushing queue...')
            self.client.indicators_create(queue)

        self.archiver and self.archiver.commit()


def main():
    p = get_argument_parser()
    p = ArgumentParser(
        description=textwrap.dedent('''\
        Env Variables:
            CSIRTG_RUNTIME_PATH
            CSIRTG_TOKEN

        example usage:
            $ csirtg-smrt --rule rules/default
            $ csirtg-smrt --rule default/csirtg.yml --feed port-scanners --remote http://localhost:5000
        '''),
        formatter_class=RawDescriptionHelpFormatter,
        prog='csirtg-smrt',
        parents=[p],
    )

    p.add_argument("-r", "--rule", help="specify the rules directory or specific rules file [default: %(default)s",
                   default=SMRT_RULES_PATH)

    p.add_argument("-f", "--feed", help="specify the feed to process")

    p.add_argument("--remote", help="specify the remote api url")
    p.add_argument('--remote-type', help="specify remote type [cif, csirtg, elasticsearch, syslog, etc]")
    p.add_argument('--client', default='stdout')

    p.add_argument('--cache', help="specify feed cache [default %(default)s]", default=SMRT_CACHE)

    p.add_argument("--limit", help="limit the number of records processed [default: %(default)s]",
                   default=None)

    p.add_argument("--token", help="specify token [default: %(default)s]", default=TOKEN)

    p.add_argument('--service', action='store_true', help="start in service mode")
    p.add_argument('--sleep', default=60)
    p.add_argument('--ignore-unknown', action='store_true')

    p.add_argument('--config', help='specify csirtg-smrt config path [default %(default)s', default=CONFIG_PATH)

    p.add_argument('--user')

    p.add_argument('--delay', help='specify initial delay', default=randint(5, 55))

    p.add_argument('--remember-path', help='specify remember db path [default: %(default)s', default=ARCHIVE_PATH)
    p.add_argument('--remember', help='remember what has been already processed', action='store_true')

    p.add_argument('--format', help='specify output format [default: %(default)s]"', default=FORMAT,
                   choices=FORMATS.keys())

    p.add_argument('--filter-indicator', help='filter for specific indicator, useful in testing')

    p.add_argument('--fireball', help='run in fireball mode, bulk+async magic', action='store_true')
    p.add_argument('--no-fetch', help='do not re-fetch if the cache exists', action='store_true')

    p.add_argument('--no-verify-ssl', help='turn TLS/SSL verification OFF', action='store_true')

    args = p.parse_args()

    o = read_config(args)
    options = vars(args)
    for v in options:
        if options[v] is None:
            options[v] = o.get(v)

    setup_logging(args)
    logger = logging.getLogger()
    logger.info('loglevel is: {}'.format(logging.getLevelName(logger.getEffectiveLevel())))

    setup_signals(__name__)

    setup_runtime_path(args.runtime_path)

    archiver = False
    if args.remember:
        archiver = Archiver(dbfile=args.remember_path)

    stop = False
    service = args.service

    verify_ssl = True
    if options.get('no_verify_ssl') or o.get('no_verify_ssl'):
        verify_ssl = False

    if service:
        r = int(args.delay)
        logger.info("random delay is {}, then running every 60min after that".format(r))
        try:
            sleep((r * 60))
        except KeyboardInterrupt:
            logger.info('shutting down')
            stop = True

    while not stop:
        if not service:
            stop = True

        data = False
        if select.select([sys.stdin, ], [], [], 0.0)[0]:
            data = sys.stdin.read()

        logger.info('starting...')

        try:
            with Smrt(options.get('token'), options.get('remote'), client=args.client, username=args.user,
                      feed=args.feed, archiver=archiver, fireball=args.fireball, no_fetch=args.no_fetch,
                      verify_ssl=verify_ssl) as s:

                s.client.ping(write=True)
                filters = {}
                if args.filter_indicator:
                    filters['indicator'] = args.filter_indicator

                indicators = []
                for r, f in s.load_feeds(args.rule, feed=args.feed):
                    for i in s.process(r, f, limit=args.limit, data=data, filters=filters):
                        if args.client == 'stdout':
                            indicators.append(i)

                if args.client == 'stdout':
                    print(FORMATS[options.get('format')](data=indicators))

                logger.info('complete')

                if args.service:
                    logger.info('sleeping for 1 hour')
                    sleep((60 * 60))

        except AuthError as e:
            logger.error(e)
            stop = True
        except RuntimeError as e:
            logger.error(e)
            if str(e).startswith('submission failed'):
                stop = True
            else:
                logging.exception('Got exception on main handler')
        except TimeoutError as e:
            logger.error(e)
            stop = True
        except KeyboardInterrupt:
            logger.info('shutting down')
            stop = True
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise e

        if archiver:
            rv = archiver.cleanup()
            logger.info('cleaning up archive: %i' % rv)

        logger.info('completed')

if __name__ == "__main__":
    main()
