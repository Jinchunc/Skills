#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import argparse
from datetime import datetime
from html import unescape
from typing import Dict, Any, Optional

import requests


def strip_html_tags(html_content: str) -> str:
    if not html_content:
        return ''
    html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<br\s*/?>', '\n', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'</p>', '\n\n', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'</div>', '\n', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'</li>', '\n', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'<[^>]+>', '', html_content)
    html_content = unescape(html_content)
    html_content = re.sub(r'\n\s*\n\s*\n', '\n\n', html_content)
    return html_content.strip()


def load_config():
    config = {}
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.txt')
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()
    return config


config = load_config()
API_BASE_URL = os.getenv('PINGKAI_API_BASE_URL', config.get('API_BASE_URL', 'https://support.pingkai.cn/api/v1'))
def resolve_credentials():
    username = os.getenv('PINGKAI_TICKET_USERNAME') or config.get('USERNAME')
    password = os.getenv('PINGKAI_TICKET_PASSWORD') or config.get('PASSWORD')
    if not username or not password:
        raise RuntimeError(
            'Missing Pingkai credentials. Set PINGKAI_TICKET_USERNAME and PINGKAI_TICKET_PASSWORD in the environment.'
        )
    return username, password


class TicketAPIClient:
    def __init__(self, base_url: str = API_BASE_URL, verbose: bool = True):
        self.base_url = str(base_url or API_BASE_URL).rstrip('/')
        self.session = requests.Session()
        self.access_token = None
        self.user_info = None
        self.username, self.password = resolve_credentials()
        self.verbose = verbose

    def api_url(self, path: str) -> str:
        normalized = '/' + str(path or '').lstrip('/')
        return f'{self.base_url}{normalized}'

    def login(self, username: str = None, password: str = None) -> bool:
        username = username or self.username
        password = password or self.password
        try:
            resp = self.session.post(self.api_url('/users/login'), json={'username': username, 'password': password}, timeout=20)
            resp.raise_for_status()
            result = resp.json()
            if result.get('base', {}).get('code') != 0:
                print(f"登录失败: {result.get('base', {}).get('message', '未知错误')}")
                return False
            self.access_token = result.get('access_token')
            self.user_info = result.get('user', {})
            self.session.headers.update({'Authorization': f'Bearer {self.access_token}'})
            return True
        except requests.exceptions.RequestException as e:
            print(f"登录请求失败: {e}")
            return False

    def _request_json(self, method: str, url: str, retry_on_401: bool = True, **kwargs):
        response = self.session.request(method, url, timeout=20, **kwargs)
        if response.status_code == 401 and retry_on_401 and self.login():
            response = self.session.request(method, url, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_ticket_by_id(self, ticket_id: str, include_internal: bool = False) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            print('请先登录')
            return None
        try:
            result = self._request_json('GET', self.api_url(f'/tickets/{ticket_id}'))
            if result.get('base', {}).get('code') != 0:
                print(f"获取工单失败: {result.get('base', {}).get('message', '未知错误')}")
                return None
            ticket = result.get('ticket')
            if ticket and not ticket.get('comments'):
                self._fill_comments(ticket, include_internal)
            return ticket
        except requests.exceptions.RequestException as e:
            print(f"获取工单请求失败: {e}")
            return None

    def get_ticket_by_alias(self, alias: int, include_internal: bool = False) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            print('请先登录')
            return None
        try:
            result = self._request_json('GET', self.api_url(f'/tickets/alias/{alias}'))
            if result.get('base', {}).get('code') != 0:
                print(f"获取工单失败: {result.get('base', {}).get('message', '未知错误')}")
                return None
            ticket = result.get('ticket')
            if ticket and not ticket.get('comments'):
                self._fill_comments(ticket, include_internal)
            return ticket
        except requests.exceptions.RequestException as e:
            print(f"获取工单请求失败: {e}")
            return None

    def list_tickets(
        self,
        page: int = 1,
        page_size: int = 100,
        order_by: str = 'created_at',
        order_direction: str = 'desc',
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            print('请先登录')
            return None

        try:
            normalized_page = max(1, int(page))
            normalized_page_size = max(1, min(int(page_size), 100))
        except (TypeError, ValueError):
            print('分页参数无效')
            return None

        params = {
            'pagination.page': normalized_page,
            'pagination.pageSize': normalized_page_size,
            'order_by': order_by or 'created_at',
            'order_direction': order_direction or 'desc',
        }
        if extra_params:
            for key, value in extra_params.items():
                if value not in (None, ''):
                    params[key] = value

        try:
            result = self._request_json('GET', self.api_url('/tickets'), params=params)
            code = result.get('code')
            if code is None:
                code = result.get('base', {}).get('code')
            if code != 0:
                message = result.get('message') or result.get('base', {}).get('message') or '未知错误'
                print(f"获取工单列表失败: {message}")
                return None
            return result
        except requests.exceptions.RequestException as e:
            print(f"获取工单列表请求失败: {e}")
            return None

    def add_working_time(
        self,
        ticket_id: str,
        date: str,
        working_time: float,
        comment: str = '',
    ) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            print('请先登录')
            return None
        payload = {
            'ticket_id': ticket_id,
            'date': date,
            'working_time': working_time,
            'comment': comment or '',
        }
        try:
            result = self._request_json('POST', self.api_url('/working-time'), json=payload)
            code = result.get('code')
            if code is None:
                code = result.get('base', {}).get('code')
            if code != 0:
                message = result.get('message') or result.get('base', {}).get('message') or '未知错误'
                print(f"添加工时失败: {message}")
                return None
            return result
        except requests.exceptions.RequestException as e:
            print(f"添加工时请求失败: {e}")
            return None

    def get_working_times(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            print('请先登录')
            return None
        try:
            result = self._request_json('GET', self.api_url('/working-time'), params={'ticket_id': ticket_id})
            code = result.get('code')
            if code is None:
                code = result.get('base', {}).get('code')
            if code != 0:
                message = result.get('message') or result.get('base', {}).get('message') or '未知错误'
                print(f"获取工时失败: {message}")
                return None
            return result
        except requests.exceptions.RequestException as e:
            print(f"获取工时请求失败: {e}")
            return None

    def _fill_comments(self, ticket: Dict[str, Any], include_internal: bool = False):
        ticket_id = ticket.get('id')
        if not ticket_id:
            return
        params = ['sort=created_at', 'order=desc', 'page_size=1000']
        if include_internal:
            params.insert(0, 'include_internal=true')
        comments_url = f"{self.api_url(f'/tickets/{ticket_id}/comments')}?{'&'.join(params)}"
        try:
            result = self._request_json('GET', comments_url)
            if result.get('base', {}).get('code') == 0:
                ticket['comments'] = result.get('comments', [])
        except requests.exceptions.RequestException:
            return

    def format_ticket_info(self, ticket: Dict[str, Any]) -> str:
        out = []
        out.append(f"工单编号: #{ticket.get('alias', 'N/A')}")
        out.append(f"标题: {ticket.get('title', 'N/A')}")
        out.append(f"状态: {ticket.get('status', 'N/A')}")
        out.append(f"优先级: {ticket.get('priority', 'N/A')}")
        if ticket.get('version_name'):
            out.append(f"版本: {ticket.get('version_name')}")
        if ticket.get('description'):
            out.append('')
            out.append('问题描述:')
            out.append(strip_html_tags(ticket.get('description', '')))
        comments = ticket.get('comments', [])
        if comments:
            out.append('')
            out.append(f'评论 ({len(comments)}条):')
            for i, c in enumerate(comments, 1):
                author = c.get('author', {}).get('full_name', 'Unknown')
                content = strip_html_tags(c.get('content', ''))
                created = c.get('created_at', '')
                out.append(f"{i}. {author} ({created})")
                if content:
                    out.append(content)
        return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(
        description='获取 Pingkai 工单详情',
        epilog='默认使用环境变量 PINGKAI_TICKET_USERNAME / PINGKAI_TICKET_PASSWORD；命令行用户名密码参数仅保留兼容用途。',
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--id', type=str, help='canonical Pingkai ticket id')
    group.add_argument('--alias', type=int, help='ticket alias, for example 8368')
    parser.add_argument('--username', type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument('--password', type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument('--url', type=str, default=API_BASE_URL, help='Pingkai API base URL')
    parser.add_argument('--json', action='store_true', help='print raw ticket JSON')
    parser.add_argument('--save', type=str, help='write output to a local file')
    parser.add_argument('--internal', action='store_true', help='include internal comments when available')
    args = parser.parse_args()

    client = TicketAPIClient(args.url, verbose=not args.json)
    if not client.login(args.username, args.password):
        sys.exit(1)
    ticket = client.get_ticket_by_id(args.id, args.internal) if args.id else client.get_ticket_by_alias(args.alias, args.internal)
    if not ticket:
        print('工单不存在或获取失败')
        sys.exit(1)
    output = json.dumps(ticket, ensure_ascii=False, indent=2) if args.json else client.format_ticket_info(ticket)
    if args.save:
        with open(args.save, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'工单信息已保存到: {args.save}')
    else:
        print(output)


if __name__ == '__main__':
    main()
