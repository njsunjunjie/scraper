# -*- coding: utf-8 -*-
# @Author: lonsty
# @Date:   2019-09-07 18:34:18
# @Last Modified by:   lonsty
# @Last Modified time: 2019-09-08 02:58:36
import json
import os
import re
import sys
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, wait, as_completed
from datetime import datetime
from queue import Empty, Queue
from threading import Thread
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import click
import requests
from bs4 import BeautifulSoup
from termcolor import colored, cprint

from scraper.utils import convert_to_safe_filename, mkdirs_if_not_exist, parse_users, retry

Scrapy = namedtuple('Scrapy', 'type author title url')
HOST_PAGE = 'https://www.zcool.com.cn'
PAGE_SUFFIX = '?myCate=0&sort=1&p={page}'
USER_SUFFIX = '/u/{id}'
SEARCH_DESIGNER_SUFFIX = '/search/designer?&word={word}'
TIMEOUT = (10, 20)
Q_TIMEOUT = 1
MAX_WORKERS = 20
RETRIES = 3


class ZCoolScraper():

    def __init__(self, user_id='', username='', destination=None, max_pages=None, spec_topics=None, max_topics=None,
                 max_workers=None, retries=None, redownload=None, override=False, proxies=None, thumbnail=False):
        self.start_time = datetime.now()
        print(f'\n - - - - - -+-+ {self.start_time.ctime()} +-+- - - - - -\n')

        self.spec_topics = spec_topics
        self.max_topics = max_topics or 'all'
        self.max_workers = max_workers or MAX_WORKERS
        self.pool = ThreadPoolExecutor(self.max_workers)
        self.override = override
        self.thumbnail = thumbnail
        self.pages = Queue()
        self.topics = Queue()
        self.images = Queue()
        self.stat = {
            'npages': 0,
            'ntopics': 0,
            'nimages': 0,
            'pages_pass': set([]),
            'pages_fail': set([]),
            'topics_pass': set([]),
            'topics_fail': set([]),
            'images_pass': set([]),
            'images_fail': set([])
        }

        if retries:
            global RETRIES
            RETRIES = retries

        if isinstance(proxies, str):
            try:
                self.proxies = json.loads(proxies)
            except Exception:
                cprint(f'Invalid proxies: {proxies}', 'yellow')
                sys.exit(1)
        else:
            self.proxies = None

        if redownload:
            self.username = self._reload_records(redownload)
            self.user_id = self._search_id_by_username(self.username)
            self.max_pages = self.pages.qsize()
            self.max_topics = self.topics.qsize()
            self.directory = os.path.abspath(os.path.join(destination or '', urlparse(HOST_PAGE).netloc,
                                                          convert_to_safe_filename(self.username)))
            self.stat.update({
                'npages': self.max_pages,
                'ntopics': self.max_topics,
                'nimages': self.images.qsize(),
            })
            print(f'{"Username".rjust(17)}: {colored(self.username, "cyan")}\n'
                  f'{"User ID".rjust(17)}: {self.user_id}\n'
                  f'{"Pages to scrapy".rjust(17)}: {self.max_pages:2d}\n'
                  f'{"Topics to scrapy".rjust(17)}: {self.max_topics:3d}\n'
                  f'{"Images to scrapy".rjust(17)}: {self.images.qsize():4d}\n'
                  f'Storage directory: {colored(self.directory, attrs=["underline"])}', end='\n\n')
            return

        self.user_id = user_id or self._search_id_by_username(username)
        self.base_url = urljoin(HOST_PAGE, USER_SUFFIX.format(id=self.user_id))

        try:
            response = requests.get(self.base_url, proxies=self.proxies, timeout=TIMEOUT)
        except Exception:
            cprint(f'Failed to connect to {self.base_url}', 'red')
            sys.exit(1)
        soup = BeautifulSoup(markup=response.text, features='html.parser')

        try:
            author = soup.find(name='div', id='body').get('data-name')
            if username and username != author:
                cprint(f'Invalid user id:「{user_id}」or username:「{username}」!', 'red')
                sys.exit(1)
            self.username = author
        except Exception:
            self.username = username or 'anonymous'
        self.directory = os.path.abspath(os.path.join(destination or '', urlparse(HOST_PAGE).netloc,
                                                      convert_to_safe_filename(self.username)))

        try:
            max_page = int(soup.find(id='laypage_0').find_all(name='a')[-2].text)
        except Exception:
            max_page = 1
        self.max_pages = min(max_pages or 9999, max_page)

        if self.spec_topics:
            topics = ', '.join(self.spec_topics)
        elif self.max_topics == 'all':
            topics = 'all'
        else:
            topics = self.max_pages * self.max_topics
        print(f'{"Username".rjust(17)}: {colored(self.username, "cyan")}\n'
              f'{"User ID".rjust(17)}: {self.user_id}\n'
              f'{"Maximum pages".rjust(17)}: {max_page}\n'
              f'{"Pages to scrapy".rjust(17)}: {self.max_pages}\n'
              f'{"Topics to scrapy".rjust(17)}: {topics}\n'
              f'Storage directory: {colored(self.directory, attrs=["underline"])}', end='\n\n')

        self.END_PARSING_TOPICS = False
        self._fetch_all()

    def _search_id_by_username(self, username):
        if not username:
            cprint('Must give an <user id> or <username>!', 'yellow')
            sys.exit(1)

        search_url = urljoin(HOST_PAGE, SEARCH_DESIGNER_SUFFIX.format(word=username))
        try:
            response = requests.get(search_url, proxies=self.proxies, timeout=TIMEOUT)
        except Exception:
            cprint(f'Failed to connect to {search_url}', 'red')
            sys.exit(1)

        author_1st = BeautifulSoup(response.text, 'html.parser').find(name='div', class_='author-info')
        if (not author_1st) or (author_1st.get('data-name') != username):
            cprint(f'Username does not exist: {username}', 'yellow')
            sys.exit(1)

        return author_1st.get('data-id')

    def _reload_records(self, file):
        with open(file, 'r', encoding='utf-8') as ff:
            for fail in json.loads(ff.read()).get('fail'):
                scrapy = Scrapy._make(fail.values())
                if scrapy.type == 'page':
                    self.pages.put(scrapy)
                elif scrapy.type == 'topic':
                    self.topics.put(scrapy)
                elif scrapy.type == 'image':
                    self.images.put(scrapy)
            return scrapy.author

    def _generate_all_pages(self):
        for i in range(1, self.max_pages + 1):
            url = urljoin(self.base_url, PAGE_SUFFIX.format(page=i))
            scrapy = Scrapy(type='page', author=self.username, title=i, url=url)
            if scrapy not in self.stat["pages_pass"]:
                self.pages.put(scrapy)

    def _fetch_all_topics(self):
        page_future = {}
        while True:
            try:
                scrapy = self.pages.get(timeout=Q_TIMEOUT)
                if scrapy not in self.stat["pages_pass"]:
                    page_future[self.pool.submit(self.parse_topics, scrapy)] = scrapy
            except Empty:
                break
            except Exception:
                continue
        for idx, future in enumerate(as_completed(page_future)):
            scrapy = page_future.get(future)
            try:
                future.result()
                self.stat["pages_pass"].add(scrapy)
            except Exception as exc:
                self.stat["pages_fail"].add(scrapy)
        self.END_PARSING_TOPICS = True

    def _fetch_all_images(self):
        image_future = {}
        while True:
            try:
                scrapy = self.topics.get(timeout=Q_TIMEOUT)
                if scrapy not in self.stat["topics_pass"]:
                    image_future[self.pool.submit(self.parse_images, scrapy)] = scrapy
            except Empty:
                if self.END_PARSING_TOPICS:
                    break
            except Exception:
                continue

        for idx, future in enumerate(as_completed(image_future)):
            scrapy = image_future.get(future)
            try:
                future.result()
                self.stat["topics_pass"].add(scrapy)
            except Exception:
                self.stat["topics_fail"].add(scrapy)

    def _fetch_all(self):
        fetch_future = [self.pool.submit(self._generate_all_pages),
                        self.pool.submit(self._fetch_all_topics),
                        self.pool.submit(self._fetch_all_images)]
        end_show_fetch = False
        t = Thread(target=self._show_fetch_status, kwargs={'end': lambda: end_show_fetch})
        t.start()
        wait(fetch_future)
        end_show_fetch = True
        t.join()

    def _show_fetch_status(self, interval=0.5, end=None):
        while True:
            status = 'Fetched Pages: {pages}\tTopics: {topics}\tImages: {images}'.format(
                pages=colored(str(self.max_pages).rjust(3), 'blue'),
                topics=colored(str(self.stat["ntopics"]).rjust(3), 'blue'),
                images=colored(str(self.stat["nimages"]).rjust(5), 'blue'))
            print(status, end='\r', flush=True)
            if (interval == 0) or (end and end()):
                print('\n')
                break
            time.sleep(interval)

    def _show_download_status(self, interval=0.5, end=None):
        while True:
            completed = len(self.stat["images_pass"]) + len(self.stat["images_fail"])
            if self.stat["nimages"] > 0:
                status = 'Time used: {time_used}\tFailed: {failed}\tCompleted: {completed}'.format(
                    time_used=colored(str(datetime.now() - self.start_time)[:-7], 'yellow'),
                    failed=colored(str(len(self.stat["images_fail"])).rjust(3), 'red'),
                    completed=colored(str(int(completed / self.stat["nimages"] * 100))
                                      + f'% ({completed}/{self.stat["nimages"]})', 'green'))
                print(status, end='\r', flush=True)
            if (interval == 0) or (end and end()):
                if self.stat["nimages"] > 0:
                    print('\n')
                break
            time.sleep(interval)

    @retry(Exception, tries=RETRIES)
    def parse_topics(self, scrapy):
        resp = requests.get(scrapy.url, proxies=self.proxies, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise Exception(f'Response status code: {resp.status_code}')

        cards = BeautifulSoup(resp.text, 'html.parser').find_all(name='a', class_='card-img-hover')
        for card in (cards if self.max_topics == 'all' else cards[:self.max_topics + 1]):
            title = card.get('title')
            if self.spec_topics and (title not in self.spec_topics):
                continue

            new_scrapy = Scrapy('topic', scrapy.author, title, card.get('href'))
            if new_scrapy not in self.stat["topics_pass"]:
                self.topics.put(new_scrapy)
                self.stat["ntopics"] += 1
        return scrapy

    @retry(Exception, tries=RETRIES)
    def parse_images(self, scrapy):
        resp = requests.get(scrapy.url, proxies=self.proxies, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise Exception(f'Response status code: {resp.status_code}')

        soup = BeautifulSoup(markup=resp.text, features='html.parser')
        for div in soup.find_all(name='div', class_='reveal-work-wrap text-center'):
            url = div.find(name='img').get('src')
            if not self.thumbnail:
                url = url.split('@')[0]  # 原图地址
            new_scrapy = Scrapy('image', scrapy.author, scrapy.title, url)
            if new_scrapy not in self.stat["images_pass"]:
                self.images.put(new_scrapy)
                self.stat["nimages"] += 1
        return scrapy

    @retry(Exception, tries=RETRIES)
    def download_image(self, scrapy):
        try:
            name = re.findall(r'(?<=/)\w*?\.(?:jpg|gif|png|bmp)', scrapy.url, re.IGNORECASE)[0]
        except IndexError:
            name = uuid4().hex + '.jpg'

        path = os.path.join(self.directory, convert_to_safe_filename(scrapy.title))
        filename = os.path.join(path, name)
        if (not self.override) and os.path.isfile(filename):
            return scrapy

        resp = requests.get(scrapy.url, proxies=self.proxies, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise Exception(f'Response status code: {resp.status_code}')

        mkdirs_if_not_exist(path)
        with open(filename, 'wb') as fi:
            fi.write(resp.content)
        return scrapy

    @retry(Exception, tries=RETRIES)
    def save_records(self):
        filename = f'{convert_to_safe_filename(self.start_time.isoformat()[:-7])}.json'
        abspath = os.path.abspath(os.path.join(self.directory, filename))
        with open(abspath, 'w', encoding='utf-8') as ff:
            records = {
                'time': self.start_time.isoformat(),
                'success': [scrapy._asdict() for scrapy in
                            (self.stat["pages_pass"] | self.stat["topics_pass"] | self.stat["images_pass"])],
                'fail': [scrapy._asdict() for scrapy in
                         (self.stat["pages_fail"] | self.stat["topics_fail"] | self.stat["images_fail"])]
            }
            ff.write(json.dumps(records, ensure_ascii=False, indent=4))
        return abspath

    def run_scraper(self):
        end_show_download = False
        t = Thread(target=self._show_download_status, kwargs={'end': lambda: end_show_download})
        t.start()

        image_futures = {}
        while True:
            try:
                scrapy = self.images.get_nowait()
                if scrapy not in self.stat["images_pass"]:
                    image_futures[self.pool.submit(self.download_image, scrapy)] = scrapy
                else:
                    pass
            except Empty:
                break
            except Exception:
                continue

        for idx, future in enumerate(as_completed(image_futures)):
            scrapy = image_futures.get(future)
            try:
                if future.result():
                    self.stat["images_pass"].add(scrapy)
                else:
                    self.stat["images_fail"].add(scrapy)
            except Exception:
                self.stat["images_fail"].add(scrapy)

        end_show_download = True
        t.join()

        saved_images = len(self.stat["images_pass"])
        failed_images = len(self.stat["images_fail"])
        if saved_images or failed_images:
            if saved_images:
                print(f'Saved {colored(saved_images, "green")} images to {colored(self.directory, attrs=["underline"])}')
            records_path = self.save_records()
            print(f'Saved records to {colored(records_path, attrs=["underline"])}')
        else:
            cprint('No images to download.', 'yellow')


@click.command()
@click.option('-u', '--usernames', 'names', help='One or more user names, separated by commas.')
@click.option('-i', '--ids', 'ids', help='One or more user ids, separated by commas.')
@click.option('-t', '--topics', 'topics', help='Specific topics of this user to download, separated by commas.')
@click.option('-d', '--destination', 'dest', help='Directory to save images.')
@click.option('-R', '--retries', 'retries', default=RETRIES, show_default=True, type=int,
              help='Repeat download for failed images.')
@click.option('-r', '--redownload', 'redownload', help='Redownload images from failed records.')
@click.option('-o', '--override', 'override', is_flag=True, default=False, show_default=True,
              help='Override existing files.')
@click.option('--thumbnail', 'thumbnail', is_flag=True, default=False, show_default=True,
              help='Download thumbnails with a maximum width of 1280px.')
@click.option('--max-pages', 'max_pages', type=int, help='Maximum pages to download.')
@click.option('--max-topics', 'max_topics', type=int, help='Maximum topics per page to download.')
@click.option('--max-workers', 'max_workers', default=MAX_WORKERS, show_default=True, type=int,
              help='Maximum thread workers.')
@click.option('--proxies', help='Use proxies to access websites.\nExample:\n\'{"http": "user:passwd'
                                '@www.example.com:port",\n"https": "user:passwd@www.example.com:port"}\'')
def zcool_command(ids, names, dest, max_pages, topics, max_topics, max_workers,
                  retries, redownload, override, proxies, thumbnail):
    """Use multi-threaded to download images from https://www.zcool.com.cn by usernames or IDs."""
    if redownload:
        scraper = ZCoolScraper(user_id='', username='', destination=dest, max_pages=max_pages, spec_topics=topics,
                               max_topics=max_topics, max_workers=max_workers, retries=retries, redownload=redownload,
                               override=override, proxies=proxies, thumbnail=thumbnail)
        scraper.run_scraper()

    elif ids or names:
        topics = topics.split(',') if topics else []
        users = parse_users(ids, names)
        for user in users:
            scraper = ZCoolScraper(user_id=user.id, username=user.name, destination=dest, max_pages=max_pages,
                                   spec_topics=topics, max_topics=max_topics, max_workers=max_workers, retries=retries,
                                   redownload=redownload, override=override, proxies=proxies)
            scraper.run_scraper()

    else:
        click.echo('Must give an <id> or <username>!')
        sys.exit(1)
