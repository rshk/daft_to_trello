# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, unicode_literals

import contextlib
import os
import shelve
import urlparse
from collections import defaultdict
from ConfigParser import NoOptionError, NoSectionError, RawConfigParser

import click
import lxml.html
import requests


DEFAULT_CONFIG_FILE = os.path.expanduser('~/.config/daft2trello.ini')
CONFIG_FILE = os.environ.get('DAFT2TRELLORC') or DEFAULT_CONFIG_FILE

REQUEST_CACHE_FILE = os.environ.get('DAFT2TRELLO_CACHEFILE')


class CustomConfigParser(RawConfigParser):
    def get_default(self, section, option, default=None):
        try:
            return self.get(section, option)
        except (NoSectionError, NoOptionError):
            return default

    def set_safe(self, section, option, value):
        if not self.has_section(section):
            self.add_section(section)
        return self.set(section, option, value)


def get_config_parser(require=True):
    configparser = CustomConfigParser()

    if os.path.exists(CONFIG_FILE):
        click.echo('Loading configuration from {}'.format(CONFIG_FILE))
        with open(CONFIG_FILE, 'r') as fp:
            configparser.readfp(fp)
    else:
        if require:
            raise RuntimeError('Configuration file does not exist')

    return configparser


class TrelloClientException(Exception):
    @classmethod
    def from_response(cls, response):
        exc = cls('HTTP Error: {}'.format(response.status_code))
        exc.response = response
        return exc


class TrelloClient(object):
    base_url = 'https://api.trello.com'

    def __init__(self, api_key, user_token):
        self.api_key = api_key
        self.user_token = user_token

    def list_user_boards(self):
        return self.get('/1/members/me/boards')

    def get_board(self, board_id, lists=None, cards=None):
        # lists: all | none | open | closed
        # cards: all | none | open | closed | visible
        kwargs = {'params': {}}
        if lists:
            kwargs['params']['lists'] = lists
        if cards:
            kwargs['params']['cards'] = cards
        return self.get('/1/boards/{}'.format(board_id), **kwargs)

    def create_card(self, list_id, name=None, desc=None, pos='bottom',
                    url_source=None):
        data = {'idList': list_id}
        if name:
            data['name'] = name
        if desc:
            data['desc'] = desc
        if pos:
            data['pos'] = pos
        if url_source:
            data['urlSource'] = url_source
        return self.post('/1/cards', data=data)

    def attach_to_card(self, card_id, file_data=None, url=None, name=None,
                       mimetype=None):
        data = {}
        if file_data:
            data['file'] = file_data
        if url:
            data['url'] = url
        if name:
            data['name'] = name
        if mimetype:
            data['mimeType'] = mimetype
        return self.post('/1/cards/{}/attachments'.format(card_id), data=data)

    def update_card(self, card_id, name=None, cover_attachment_id=None):
        data = {}
        if name is not None:
            data['name'] = name
        if cover_attachment_id is not None:
            if cover_attachment_id is False:
                cover_attachment_id = ''
            data['cover_attachment_id'] = cover_attachment_id
        return self.put('/1/cards/{}'.format(card_id), data=data)

    def request(self, method, path, **kwargs):
        url = urlparse.urljoin(self.base_url, path)

        if method.upper() in ('GET', 'HEAD', 'OPTIONS'):
            params_arg = 'params'
        else:
            params_arg = 'data'

        if params_arg not in kwargs:
            kwargs[params_arg] = {}
        kwargs[params_arg]['key'] = self.api_key
        kwargs[params_arg]['token'] = self.user_token

        response = requests.request(method, url, **kwargs)
        if not response.ok:
            raise TrelloClientException.from_response(response)
        if response.status_code == 204:
            return None  # no content
        return response.json()

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def put(self, path, **kwargs):
        return self.request('PUT', path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request('DELETE', path, **kwargs)

    @classmethod
    def from_config(cls, configparser):
        api_key = configparser.get('trello', 'api_key')
        user_token = configparser.get('trello', 'user_token')
        return cls(api_key, user_token)


class CachedHttpClient(object):
    """
    Mostly for convenience during development of the scraper :)
    """

    @contextlib.contextmanager
    def _shelf(self, *a, **kw):
        shelf = shelve.open(*a, **kw)
        yield shelf
        shelf.close()

    def get(self, url):
        url = url.encode('utf-8')

        if REQUEST_CACHE_FILE:
            with self._shelf(REQUEST_CACHE_FILE) as shelf:
                if url in shelf:
                    # click.echo('Using cached version')
                    return shelf[url]

        response = requests.get(url)
        assert response.ok

        if REQUEST_CACHE_FILE:
            with self._shelf(REQUEST_CACHE_FILE) as shelf:
                shelf[url] = response.content

        return response.content


@click.group()
@click.option('--verbose', default=False)
def cli(verbose):
    # todo: configure logging, if verbose
    pass


@cli.command()
def configure():
    configparser = get_config_parser(require=False)
    _configure(configparser)
    click.echo('Writing configuration to {}'.format(CONFIG_FILE))
    with open(CONFIG_FILE, 'w') as fp:
        configparser.write(fp)


def _configure(configparser):
    TRELLO_API_KEY = configparser.get_default('trello', 'api_key')
    if not TRELLO_API_KEY:
        click.echo(
            'A Trello API key is required. Please visit '
            'https://trello.com/app-key in a browser and paste the '
            'API key (value in the first box) here.')
        TRELLO_API_KEY = click.prompt('API Key')
        configparser.set_safe('trello', 'api_key', TRELLO_API_KEY)

    TRELLO_USER_TOKEN = configparser.get_default('trello', 'user_token')
    url = (
        'https://trello.com/1/authorize?key={}&name=Daft+to+Trello'
        '&expiration=never&response_type=token&scope=read,write'
        .format(TRELLO_API_KEY))
    if not TRELLO_USER_TOKEN:
        click.echo(
            'A Trello user Token is required to access the board. '
            'Please visit the following url:\n{url}\n'
            'in a browser and paste the obtained token below.'
            .format(url=url))
        TRELLO_USER_TOKEN = click.prompt('User token')
        configparser.set_safe('trello', 'user_token', TRELLO_USER_TOKEN)

    trello_client = TrelloClient(TRELLO_API_KEY, TRELLO_USER_TOKEN)

    TRELLO_BOARD = configparser.get_default('trello', 'board')
    if not TRELLO_BOARD:
        click.echo(
            'No board was selected. Please choose one, or type "create" '
            'to create one from scratch.\n')
        boards = trello_client.list_user_boards()
        for board in boards:
            click.echo('    {0[id]} {0[name]}'.format(board))
        TRELLO_BOARD = click.prompt('Your choice')
        if TRELLO_BOARD == 'create':
            TRELLO_BOARD = _create_trello_board(trello_client)
        configparser.set_safe('trello', 'board', TRELLO_BOARD)
    _validate_trello_board(trello_client, TRELLO_BOARD)


def _create_trello_board(trello_client):
    pass


def _validate_trello_board(trello_client, board_id):
    # todo: check that lists are in place etc.
    pass


@cli.command()
def display_board():
    configparser = get_config_parser(require=False)
    trello_client = TrelloClient.from_config(configparser)

    board_id = configparser.get('trello', 'board')
    board = trello_client.get_board(board_id, lists='all', cards='all')

    # click.echo(json.dumps(board, indent=4))
    # click.echo('--------------------')

    click.echo('Id: {}'.format(board['id']))
    click.echo('Name: {}'.format(board['name']))

    cards_by_list = defaultdict(list)
    for card in board['cards']:
        cards_by_list[card['idList']].append(card)

    for b_list in board['lists']:
        click.echo('    {0[id]} {0[name]}'.format(b_list))
        for card in cards_by_list[b_list['id']]:
            click.echo('        {0[id]} {0[name]}'.format(card))


@cli.command()
@click.argument('url')
def scrape_daft(url):
    data = scrape_daft_page(url)
    # click.echo(json.dumps(data, indent=4))
    card_title = ('{0[title]} - {0[beds]} / {0[baths]} - {0[price]}'
                  .format(data))
    click.echo('Card title: {}'.format(card_title))
    click.echo('Card URL: {}'.format(data['url']))
    click.echo('Card cover pic: {}'.format(data['image']))
    click.echo('Description:\n{}'.format(data['description']))


@cli.command()
@click.argument('url')
def import_ad(url):
    data = scrape_daft_page(url)
    configparser = get_config_parser(require=False)
    trello_client = TrelloClient.from_config(configparser)

    board_id = configparser.get('trello', 'board')
    board = trello_client.get_board(board_id, lists='all', cards='all')

    target_list_id = board['lists'][0]['id']

    card_title = ('{0[title]} - {0[beds]} / {0[baths]} - {0[price]}'
                  .format(data))
    click.echo('Card title: {}'.format(card_title))
    click.echo('Card URL: {}'.format(data['url']))
    click.echo('Card cover pic: {}'.format(data['image']))

    card = trello_client.create_card(
        target_list_id, name=card_title,
        desc=data['description'])

    img_att = trello_client.attach_to_card(card['id'], url=data['image'])
    trello_client.update_card(card['id'], cover_attachment_id=img_att['id'])
    trello_client.attach_to_card(card['id'], url=data['url'])


def scrape_daft_page(url):
    info = {'url': url}

    http_client = CachedHttpClient()
    data = http_client.get(url)
    html = lxml.html.fromstring(data)

    content_tag = html.xpath('//div[@id="content"]')[0]

    title_tag = content_tag.cssselect('.smi-info h1')[0]
    info['title'] = title_tag.text

    image_tag = html.cssselect('#smi-gallery-img-main img')[0]
    image_src = image_tag.attrib['src']
    if image_src.startswith('//'):
        image_src = 'https:' + image_src
    info['image'] = image_src

    price_tag = html.cssselect('#smi-price-string')[0]
    info['price'] = price_tag.text

    header_text = html.cssselect('#smi-summary-items .header_text')
    hdrtext = [t.text for t in header_text]

    info['beds'] = hdrtext[1]
    info['baths'] = hdrtext[2]

    info['description'] = '\n\n'.join(
        elem.text_content()
        for elem in html.cssselect('#smi-tab-overview .description_block'))
    # info['description'] = content_tag.cssselect('.overview')[0].text

    return info


if __name__ == '__main__':
    cli()
