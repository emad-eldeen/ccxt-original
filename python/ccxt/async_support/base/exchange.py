# -*- coding: utf-8 -*-

# -----------------------------------------------------------------------------

__version__ = '4.0.112'

# -----------------------------------------------------------------------------

import asyncio
import concurrent.futures
import socket
import certifi
import aiohttp
import ssl
import sys
import yarl
import math
from typing import Any, Optional, List

# -----------------------------------------------------------------------------

from ccxt.async_support.base.throttler import Throttler

# -----------------------------------------------------------------------------

from ccxt.base.errors import BaseError, BadSymbol, BadRequest, BadResponse, AuthenticationError, ExchangeError, ExchangeNotAvailable, RequestTimeout, NotSupported, NullResponse, InvalidOrder, InvalidAddress
from ccxt.base.decimal_to_precision import TRUNCATE, ROUND, TICK_SIZE, DECIMAL_PLACES, SIGNIFICANT_DIGITS
from ccxt.base.types import OrderType, OrderSide, IndexType, Balance, Trade

# -----------------------------------------------------------------------------

from ccxt.base.exchange import Exchange as BaseExchange, ArgumentsRequired
from ccxt.base.precise import Precise

# -----------------------------------------------------------------------------

from ccxt.async_support.base.ws.functions import inflate, inflate64, gunzip
from ccxt.async_support.base.ws.fast_client import FastClient
from ccxt.async_support.base.ws.future import Future
from ccxt.async_support.base.ws.order_book import OrderBook, IndexedOrderBook, CountedOrderBook


# -----------------------------------------------------------------------------

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None

# -----------------------------------------------------------------------------

__all__ = [
    'BaseExchange',
    'Exchange',
]

# -----------------------------------------------------------------------------


class Exchange(BaseExchange):
    synchronous = False
    streaming = {
        'maxPingPongMisses': 2,
        'keepAlive': 30000
    }
    ping = None
    newUpdates = True
    clients = {}

    def __init__(self, config={}):
        if 'asyncio_loop' in config:
            self.asyncio_loop = config['asyncio_loop']
        self.aiohttp_trust_env = config.get('aiohttp_trust_env', self.aiohttp_trust_env)
        self.verify = config.get('verify', self.verify)
        self.own_session = 'session' not in config
        self.cafile = config.get('cafile', certifi.where())
        super(Exchange, self).__init__(config)
        self.throttle = None
        self.init_rest_rate_limiter()
        self.markets_loading = None
        self.reloading_markets = False

    def init_rest_rate_limiter(self):
        self.throttle = Throttler(self.tokenBucket, self.asyncio_loop)

    def get_event_loop(self):
        return self.asyncio_loop

    def get_session(self):
        return self.session

    def __del__(self):
        if self.session is not None:
            self.logger.warning(self.id + " requires to release all resources with an explicit call to the .close() coroutine. If you are using the exchange instance with async coroutines, add `await exchange.close()` to your code into a place when you're done with the exchange and don't need the exchange instance anymore (at the end of your async coroutine).")

    if sys.version_info >= (3, 5):
        async def __aenter__(self):
            self.open()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            await self.close()

    def open(self):
        if self.asyncio_loop is None:
            if sys.version_info >= (3, 7):
                self.asyncio_loop = asyncio.get_running_loop()
            else:
                self.asyncio_loop = asyncio.get_event_loop()
            self.throttle.loop = self.asyncio_loop
        if self.own_session and self.session is None:
            # Create our SSL context object with our CA cert file
            context = ssl.create_default_context(cafile=self.cafile) if self.verify else self.verify
            # Pass this SSL context to aiohttp and create a TCPConnector
            connector = aiohttp.TCPConnector(ssl=context, loop=self.asyncio_loop, enable_cleanup_closed=True)
            self.session = aiohttp.ClientSession(loop=self.asyncio_loop, connector=connector, trust_env=self.aiohttp_trust_env)

    async def close(self):
        await self.ws_close()
        if self.session is not None:
            if self.own_session:
                await self.session.close()
            self.session = None

    async def fetch(self, url, method='GET', headers=None, body=None):
        """Perform a HTTP request and return decoded JSON data"""

        # ##### PROXY & HEADERS #####
        request_headers = self.prepare_request_headers(headers)
        # proxy "url"
        proxyUrl = self.check_proxy_url_settings(url, method, headers, body)
        if proxyUrl is not None:
            request_headers.update({'Origin': self.origin})
            url = proxyUrl + url
        # proxy agents
        final_proxy = None  # set default
        final_session = None
        httpProxy, httpsProxy, socksProxy = self.check_proxy_settings(url, method, headers, body)
        if httpProxy:
            final_proxy = httpProxy
        elif httpsProxy:
            final_proxy = httpsProxy
        elif socksProxy:
            if ProxyConnector is None:
                raise NotSupported(self.id + ' - to use SOCKS proxy with ccxt, you need "aiohttp_socks" module that can be installed by "pip install aiohttp_socks"')
            # Create our SSL context object with our CA cert file
            context = ssl.create_default_context(cafile=self.cafile) if self.verify else self.verify
            connector = ProxyConnector.from_url(
                socksProxy,
                # extra args copied from self.open()
                ssl=context,
                loop=self.asyncio_loop,
                enable_cleanup_closed=True
            )
            # override session
            final_session = aiohttp.ClientSession(loop=self.asyncio_loop, connector=connector, trust_env=self.aiohttp_trust_env)
        # add aiohttp_proxy for python as exclusion
        elif self.aiohttp_proxy:
            final_proxy = self.aiohttp_proxy
            
        proxyAgentSet = final_proxy is not None or socksProxy is not None
        self.checkConflictingProxies (proxyAgentSet, proxyUrl)
        # avoid old proxies mixing
        if (self.aiohttp_proxy is not None) and (proxyUrl is not None or httpProxy is not None or httpsProxy is not None or socksProxy is not None):
            raise NotSupported(self.id + ' you have set multiple proxies, please use one or another')

        # log
        if self.verbose:
            self.log("\nfetch Request:", self.id, method, url, "RequestHeaders:", request_headers, "RequestBody:", body)
        self.logger.debug("%s %s, Request: %s %s", method, url, headers, body)
        # end of proxies & headers

        request_body = body
        encoded_body = body.encode() if body else None
        self.open()
        session_method = getattr(final_session if final_session is not None else self.session, method.lower())

        http_response = None
        http_status_code = None
        http_status_text = None
        json_response = None
        try:
            async with session_method(yarl.URL(url, encoded=True),
                                      data=encoded_body,
                                      headers=request_headers,
                                      timeout=(self.timeout / 1000),
                                      proxy=final_proxy) as response:
                http_response = await response.text(errors='replace')
                # CIMultiDictProxy
                raw_headers = response.headers
                headers = {}
                for header in raw_headers:
                    if header in headers:
                        headers[header] = headers[header] + ', ' + raw_headers[header]
                    else:
                        headers[header] = raw_headers[header]
                http_status_code = response.status
                http_status_text = response.reason
                http_response = self.on_rest_response(http_status_code, http_status_text, url, method, headers, http_response, request_headers, request_body)
                json_response = self.parse_json(http_response)
                if self.enableLastHttpResponse:
                    self.last_http_response = http_response
                if self.enableLastResponseHeaders:
                    self.last_response_headers = headers
                if self.enableLastJsonResponse:
                    self.last_json_response = json_response
                if self.verbose:
                    self.log("\nfetch Response:", self.id, method, url, http_status_code, "ResponseHeaders:", headers, "ResponseBody:", http_response)
                self.logger.debug("%s %s, Response: %s %s %s", method, url, http_status_code, headers, http_response)

        except socket.gaierror as e:
            details = ' '.join([self.id, method, url])
            raise ExchangeNotAvailable(details) from e

        except (concurrent.futures.TimeoutError, asyncio.TimeoutError) as e:
            details = ' '.join([self.id, method, url])
            raise RequestTimeout(details) from e

        except aiohttp.ClientConnectionError as e:
            details = ' '.join([self.id, method, url])
            raise ExchangeNotAvailable(details) from e

        except aiohttp.ClientError as e:  # base exception class
            details = ' '.join([self.id, method, url])
            raise ExchangeError(details) from e

        self.handle_errors(http_status_code, http_status_text, url, method, headers, http_response, json_response, request_headers, request_body)
        self.handle_http_status_code(http_status_code, http_status_text, url, method, http_response)
        if json_response is not None:
            return json_response
        if self.is_text_response(headers):
            return http_response
        if http_response == '' or http_response is None:
            return http_response
        return response.content

    async def load_markets_helper(self, reload=False, params={}):
        if not reload:
            if self.markets:
                if not self.markets_by_id:
                    return self.set_markets(self.markets)
                return self.markets
        currencies = None
        if self.has['fetchCurrencies'] is True:
            currencies = await self.fetch_currencies()
        markets = await self.fetch_markets(params)
        return self.set_markets(markets, currencies)

    async def load_markets(self, reload=False, params={}):
        if (reload and not self.reloading_markets) or not self.markets_loading:
            self.reloading_markets = True
            coroutine = self.load_markets_helper(reload, params)
            # coroutines can only be awaited once so we wrap it in a task
            self.markets_loading = asyncio.ensure_future(coroutine)
        try:
            result = await self.markets_loading
        except Exception as e:
            self.reloading_markets = False
            self.markets_loading = None
            raise e
        self.reloading_markets = False
        return result

    async def fetch_fees(self):
        trading = {}
        funding = {}
        if self.has['fetchTradingFees']:
            trading = await self.fetch_trading_fees()
        if self.has['fetchFundingFees']:
            funding = await self.fetch_funding_fees()
        return {
            'trading': trading,
            'funding': funding,
        }

    async def load_fees(self, reload=False):
        if not reload:
            if self.loaded_fees != Exchange.loaded_fees:
                return self.loaded_fees
        self.loaded_fees = self.deep_extend(self.loaded_fees, await self.fetch_fees())
        return self.loaded_fees

    async def fetch_markets(self, params={}):
        # markets are returned as a list
        # currencies are returned as a dict
        # this is for historical reasons
        # and may be changed for consistency later
        return self.to_array(self.markets)

    async def fetch_currencies(self, params={}):
        # markets are returned as a list
        # currencies are returned as a dict
        # this is for historical reasons
        # and may be changed for consistency later
        return self.currencies

    async def fetchOHLCVC(self, symbol, timeframe='1m', since=None, limit=None, params={}):
        return await self.fetch_ohlcvc(symbol, timeframe, since, limit, params)

    async def fetch_full_tickers(self, symbols=None, params={}):
        return await self.fetch_tickers(symbols, params)

    async def sleep(self, milliseconds):
        return await asyncio.sleep(milliseconds / 1000)

    async def spawn_async(self, method, *args):
        try:
            await method(*args)
        except Exception:
            # todo: handle spawned errors
            pass

    async def delay_async(self, timeout, method, *args):
        await self.sleep(timeout)
        try:
            await method(*args)
        except Exception:
            # todo: handle spawned errors
            pass

    def spawn(self, method, *args):
        def callback(asyncio_future):
            exception = asyncio_future.exception()
            if exception is None:
                future.resolve(asyncio_future.result())
            else:
                future.reject(exception)
        future = Future()
        task = self.asyncio_loop.create_task(method(*args))
        task.add_done_callback(callback)
        return future

    #  -----------------------------------------------------------------------
    #  WS/PRO code

    @staticmethod
    def inflate(data):
        return inflate(data)

    @staticmethod
    def inflate64(data):
        return inflate64(data)

    @staticmethod
    def gunzip(data):
        return gunzip(data)

    def order_book(self, snapshot={}, depth=None):
        return OrderBook(snapshot, depth)

    def indexed_order_book(self, snapshot={}, depth=None):
        return IndexedOrderBook(snapshot, depth)

    def counted_order_book(self, snapshot={}, depth=None):
        return CountedOrderBook(snapshot, depth)

    def client(self, url):
        self.clients = self.clients or {}
        if url not in self.clients:
            on_message = self.handle_message
            on_error = self.on_error
            on_close = self.on_close
            on_connected = self.on_connected
            # decide client type here: aiohttp ws / websockets / signalr / socketio
            ws_options = self.safe_value(self.options, 'ws', {})
            options = self.extend(self.streaming, {
                'log': getattr(self, 'log'),
                'ping': getattr(self, 'ping', None),
                'verbose': self.verbose,
                'throttle': Throttler(self.tokenBucket, self.asyncio_loop),
                'asyncio_loop': self.asyncio_loop,
            }, ws_options)
            self.clients[url] = FastClient(url, on_message, on_error, on_close, on_connected, options)
        return self.clients[url]

    def delay(self, timeout, method, *args):
        return self.asyncio_loop.call_later(timeout / 1000, self.spawn, method, *args)

    def handle_message(self, client, message):
        always = True
        if always:
            raise NotSupported(self.id + '.handle_message() not implemented yet')
        return {}

    def watch(self, url, message_hash, message=None, subscribe_hash=None, subscription=None):
        backoff_delay = 0
        client = self.client(url)
        if subscribe_hash is None and message_hash in client.futures:
            return client.futures[message_hash]
        future = client.future(message_hash)

        subscribed = client.subscriptions.get(subscribe_hash)

        if not subscribed:
            client.subscriptions[subscribe_hash] = subscription or True

        # base exchange self.open starts the aiohttp Session in an async context
        self.open()
        connected = client.connected if client.connected.done() \
            else asyncio.ensure_future(client.connect(self.session, backoff_delay))

        def after(fut):
            # todo: decouple signing from subscriptions
            options = self.safe_value(self.options, 'ws')
            cost = self.safe_value(options, 'cost', 1)
            if message:
                async def send_message():
                    if self.enableRateLimit:
                        await client.throttle(cost)
                    try:
                        await client.send(message)
                    except ConnectionError as e:
                        del client.subscriptions[subscribe_hash]
                        future.reject(e)
                asyncio.ensure_future(send_message())

        if not subscribed:
            try:
                connected.add_done_callback(after)
            except Exception as e:
                del client.subscriptions[subscribe_hash]
                future.reject(e)

        return future

    def on_connected(self, client, message=None):
        # for user hooks
        # print('Connected to', client.url)
        pass

    def on_error(self, client, error):
        if client.url in self.clients and self.clients[client.url].error:
            del self.clients[client.url]

    def on_close(self, client, error):
        if client.error:
            # connection closed due to an error
            pass
        else:
            # server disconnected a working connection
            if client.url in self.clients:
                del self.clients[client.url]

    async def ws_close(self):
        if self.clients:
            await asyncio.wait([asyncio.create_task(client.close()) for client in self.clients.values()], return_when=asyncio.ALL_COMPLETED)
            for url in self.clients.copy():
                del self.clients[url]

    async def load_order_book(self, client, messageHash, symbol, limit=None, params={}):
        if symbol not in self.orderbooks:
            client.reject(ExchangeError(self.id + ' loadOrderBook() orderbook is not initiated'), messageHash)
            return
        try:
            maxRetries = self.handle_option('watchOrderBook', 'maxRetries', 3)
            tries = 0
            stored = self.orderbooks[symbol]
            while tries < maxRetries:
                cache = stored.cache
                order_book = await self.fetch_order_book(symbol, limit, params)
                index = self.get_cache_index(order_book, cache)
                if index >= 0:
                    stored.reset(order_book)
                    self.handle_deltas(stored, cache[index:])
                    cache.clear()
                    client.resolve(stored, messageHash)
                    return
                tries += 1
            client.reject(ExchangeError(self.id + ' nonce is behind cache after ' + str(maxRetries) + ' tries.'), messageHash)
            del self.clients[client.url]
        except BaseError as e:
            client.reject(e, messageHash)
            await self.load_order_book(client, messageHash, symbol, limit, params)

    def format_scientific_notation_ftx(self, n):
        if n == 0:
            return '0e-00'
        return format(n, 'g')

    # ########################################################################
    # ########################################################################
    # ########################################################################
    # ########################################################################
    # ########                        ########                        ########
    # ########                        ########                        ########
    # ########                        ########                        ########
    # ########                        ########                        ########
    # ########        ########################        ########################
    # ########        ########################        ########################
    # ########        ########################        ########################
    # ########        ########################        ########################
    # ########                        ########                        ########
    # ########                        ########                        ########
    # ########                        ########                        ########
    # ########                        ########                        ########
    # ########################################################################
    # ########################################################################
    # ########################################################################
    # ########################################################################
    # ########        ########        ########                        ########
    # ########        ########        ########                        ########
    # ########        ########        ########                        ########
    # ########        ########        ########                        ########
    # ################        ########################        ################
    # ################        ########################        ################
    # ################        ########################        ################
    # ################        ########################        ################
    # ########        ########        ################        ################
    # ########        ########        ################        ################
    # ########        ########        ################        ################
    # ########        ########        ################        ################
    # ########################################################################
    # ########################################################################
    # ########################################################################
    # ########################################################################

    # METHODS BELOW THIS LINE ARE TRANSPILED FROM JAVASCRIPT TO PYTHON AND PHP

    def handle_deltas(self, orderbook, deltas):
        for i in range(0, len(deltas)):
            self.handle_delta(orderbook, deltas[i])

    def handle_delta(self, bookside, delta):
        raise NotSupported(self.id + ' handleDelta not supported yet')

    def get_cache_index(self, orderbook, deltas):
        # return the first index of the cache that can be applied to the orderbook or -1 if not possible
        return -1

    def find_timeframe(self, timeframe, timeframes=None):
        if timeframes is None:
            timeframes = self.timeframes
        keys = list(timeframes.keys())
        for i in range(0, len(keys)):
            key = keys[i]
            if timeframes[key] == timeframe:
                return key
        return None

    def check_proxy_settings(self, url, method, headers, body):
        proxyUrl = self.proxyUrl if (self.proxyUrl is not None) else self.proxy_url
        proxyUrlCallback = self.proxyUrlCallback if (self.proxyUrlCallback is not None) else self.proxy_url_callback
        if proxyUrlCallback is not None:
            proxyUrl = proxyUrlCallback(url, method, headers, body)
        # backwards-compatibility
        if self.proxy is not None:
            if callable(self.proxy):
                proxyUrl = self.proxy(url, method, headers, body)
            else:
                proxyUrl = self.proxy
        httpProxy = self.httpProxy if (self.httpProxy is not None) else self.http_proxy
        httpProxyCallback = self.httpProxyCallback if (self.httpProxyCallback is not None) else self.http_proxy_callback
        if httpProxyCallback is not None:
            httpProxy = httpProxyCallback(url, method, headers, body)
        httpsProxy = self.httpsProxy if (self.httpsProxy is not None) else self.https_proxy
        httpsProxyCallback = self.httpsProxyCallback if (self.httpsProxyCallback is not None) else self.https_proxy_callback
        if httpsProxyCallback is not None:
            httpsProxy = httpsProxyCallback(url, method, headers, body)
        socksProxy = self.socksProxy if (self.socksProxy is not None) else self.socks_proxy
        socksProxyCallback = self.socksProxyCallback if (self.socksProxyCallback is not None) else self.socks_proxy_callback
        if socksProxyCallback is not None:
            socksProxy = socksProxyCallback(url, method, headers, body)
        val = 0
        if proxyUrl is not None:
            val = val + 1
        if proxyUrlCallback is not None:
            val = val + 1
        if httpProxy is not None:
            val = val + 1
        if httpProxyCallback is not None:
            val = val + 1
        if httpsProxy is not None:
            val = val + 1
        if httpsProxyCallback is not None:
            val = val + 1
        if socksProxy is not None:
            val = val + 1
        if socksProxyCallback is not None:
            val = val + 1
        if val > 1:
            raise ExchangeError(self.id + ' you have multiple conflicting proxy settings, please use only one from : proxyUrl, httpProxy, httpsProxy, socksProxy, userAgent')
        return [proxyUrl, httpProxy, httpsProxy, socksProxy]

    def find_message_hashes(self, client, element: str):
        result = []
        messageHashes = list(client.futures.keys())
        for i in range(0, len(messageHashes)):
            messageHash = messageHashes[i]
            if messageHash.find(element) >= 0:
                result.append(messageHash)
        return result

    def filter_by_limit(self, array: List[object], limit: Optional[int] = None, key: IndexType = 'timestamp'):
        if self.valueIsDefined(limit):
            arrayLength = len(array)
            if arrayLength > 0:
                ascending = True
                if (key in array[0]):
                    first = array[0][key]
                    last = array[arrayLength - 1][key]
                    if first is not None and last is not None:
                        ascending = first <= last  # True if array is sorted in ascending order based on 'timestamp'
                array = self.arraySlice(array, -limit) if ascending else self.arraySlice(array, 0, limit)
        return array

    def filter_by_since_limit(self, array: List[object], since: Optional[int] = None, limit: Optional[int] = None, key: IndexType = 'timestamp', tail=False):
        sinceIsDefined = self.valueIsDefined(since)
        parsedArray = self.to_array(array)
        result = parsedArray
        if sinceIsDefined:
            result = []
            for i in range(0, len(parsedArray)):
                entry = parsedArray[i]
                value = self.safe_value(entry, key)
                if value and (value >= since):
                    result.append(entry)
        if tail and limit is not None:
            return self.arraySlice(result, -limit)
        return self.filter_by_limit(result, limit, key)

    def filter_by_value_since_limit(self, array: List[object], field: IndexType, value=None, since: Optional[int] = None, limit: Optional[int] = None, key='timestamp', tail=False):
        valueIsDefined = self.valueIsDefined(value)
        sinceIsDefined = self.valueIsDefined(since)
        parsedArray = self.to_array(array)
        result = parsedArray
        # single-pass filter for both symbol and since
        if valueIsDefined or sinceIsDefined:
            result = []
            for i in range(0, len(parsedArray)):
                entry = parsedArray[i]
                entryFiledEqualValue = entry[field] == value
                firstCondition = entryFiledEqualValue if valueIsDefined else True
                entryKeyValue = self.safe_value(entry, key)
                entryKeyGESince = (entryKeyValue) and since and (entryKeyValue >= since)
                secondCondition = entryKeyGESince if sinceIsDefined else True
                if firstCondition and secondCondition:
                    result.append(entry)
        if tail and limit is not None:
            return self.arraySlice(result, -limit)
        return self.filter_by_limit(result, limit, key)

    def set_sandbox_mode(self, enabled):
        if enabled:
            if 'test' in self.urls:
                if isinstance(self.urls['api'], str):
                    self.urls['apiBackup'] = self.urls['api']
                    self.urls['api'] = self.urls['test']
                else:
                    self.urls['apiBackup'] = self.clone(self.urls['api'])
                    self.urls['api'] = self.clone(self.urls['test'])
            else:
                raise NotSupported(self.id + ' does not have a sandbox URL')
        elif 'apiBackup' in self.urls:
            if isinstance(self.urls['api'], str):
                self.urls['api'] = self.urls['apiBackup']
            else:
                self.urls['api'] = self.clone(self.urls['apiBackup'])
            newUrls = self.omit(self.urls, 'apiBackup')
            self.urls = newUrls

    def sign(self, path, api: Any = 'public', method='GET', params={}, headers: Optional[Any] = None, body: Optional[Any] = None):
        return {}

    async def fetch_accounts(self, params={}):
        raise NotSupported(self.id + ' fetchAccounts() is not supported yet')

    async def fetch_trades(self, symbol: str, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchTrades() is not supported yet')

    async def fetch_trades_ws(self, symbol: str, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchTradesWs() is not supported yet')

    async def watch_trades(self, symbol: str, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchTrades() is not supported yet')

    async def watch_trades_for_symbols(self, symbols: List[str], since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchTradesForSymbols() is not supported yet')

    async def watch_ohlcv_for_symbols(self, symbolsAndTimeframes: List[List[str]], since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchOHLCVForSymbols() is not supported yet')

    async def watch_order_book_for_symbols(self, symbols: List[str], limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchOrderBookForSymbols() is not supported yet')

    async def fetch_deposit_addresses(self, codes: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchDepositAddresses() is not supported yet')

    async def fetch_order_book(self, symbol: str, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchOrderBook() is not supported yet')

    async def fetch_rest_order_book_safe(self, symbol, limit=None, params={}):
        fetchSnapshotMaxRetries = self.handleOption('watchOrderBook', 'maxRetries', 3)
        for i in range(0, fetchSnapshotMaxRetries):
            try:
                orderBook = await self.fetch_order_book(symbol, limit, params)
                return orderBook
            except Exception as e:
                if (i + 1) == fetchSnapshotMaxRetries:
                    raise e
        return None

    async def watch_order_book(self, symbol: str, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchOrderBook() is not supported yet')

    async def fetch_time(self, params={}):
        raise NotSupported(self.id + ' fetchTime() is not supported yet')

    async def fetch_trading_limits(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchTradingLimits() is not supported yet')

    def parse_ticker(self, ticker: object, market=None):
        raise NotSupported(self.id + ' parseTicker() is not supported yet')

    def parse_deposit_address(self, depositAddress, currency=None):
        raise NotSupported(self.id + ' parseDepositAddress() is not supported yet')

    def parse_trade(self, trade: object, market=None):
        raise NotSupported(self.id + ' parseTrade() is not supported yet')

    def parse_transaction(self, transaction, currency=None):
        raise NotSupported(self.id + ' parseTransaction() is not supported yet')

    def parse_transfer(self, transfer, currency=None):
        raise NotSupported(self.id + ' parseTransfer() is not supported yet')

    def parse_account(self, account):
        raise NotSupported(self.id + ' parseAccount() is not supported yet')

    def parse_ledger_entry(self, item, currency=None):
        raise NotSupported(self.id + ' parseLedgerEntry() is not supported yet')

    def parse_order(self, order, market=None):
        raise NotSupported(self.id + ' parseOrder() is not supported yet')

    async def fetch_borrow_rates(self, params={}):
        raise NotSupported(self.id + ' fetchBorrowRates() is not supported yet')

    def parse_market_leverage_tiers(self, info, market=None):
        raise NotSupported(self.id + ' parseMarketLeverageTiers() is not supported yet')

    async def fetch_leverage_tiers(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchLeverageTiers() is not supported yet')

    def parse_position(self, position, market=None):
        raise NotSupported(self.id + ' parsePosition() is not supported yet')

    def parse_funding_rate_history(self, info, market=None):
        raise NotSupported(self.id + ' parseFundingRateHistory() is not supported yet')

    def parse_borrow_interest(self, info, market=None):
        raise NotSupported(self.id + ' parseBorrowInterest() is not supported yet')

    def parse_ws_trade(self, trade, market=None):
        raise NotSupported(self.id + ' parseWsTrade() is not supported yet')

    def parse_ws_order(self, order, market=None):
        raise NotSupported(self.id + ' parseWsOrder() is not supported yet')

    def parse_ws_order_trade(self, trade, market=None):
        raise NotSupported(self.id + ' parseWsOrderTrade() is not supported yet')

    def parse_ws_ohlcv(self, ohlcv, market=None):
        return self.parse_ohlcv(ohlcv, market)

    async def fetch_funding_rates(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchFundingRates() is not supported yet')

    async def transfer(self, code: str, amount, fromAccount, toAccount, params={}):
        raise NotSupported(self.id + ' transfer() is not supported yet')

    async def withdraw(self, code: str, amount, address, tag=None, params={}):
        raise NotSupported(self.id + ' withdraw() is not supported yet')

    async def create_deposit_address(self, code: str, params={}):
        raise NotSupported(self.id + ' createDepositAddress() is not supported yet')

    async def set_leverage(self, leverage, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' setLeverage() is not supported yet')

    def parse_to_int(self, number):
        # Solve Common intmisuse ex: int((since / str(1000)))
        # using a number which is not valid in ts
        stringifiedNumber = str(number)
        convertedNumber = float(stringifiedNumber)
        return int(convertedNumber)

    def after_construct(self):
        self.create_networks_by_id_object()

    def create_networks_by_id_object(self):
        # automatically generate network-id-to-code mappings
        networkIdsToCodesGenerated = self.invert_flat_string_dictionary(self.safe_value(self.options, 'networks', {}))  # invert defined networks dictionary
        self.options['networksById'] = self.extend(networkIdsToCodesGenerated, self.safe_value(self.options, 'networksById', {}))  # support manually overriden "networksById" dictionary too

    def get_default_options(self):
        return {
            'defaultNetworkCodeReplacements': {
                'ETH': {'ERC20': 'ETH'},
                'TRX': {'TRC20': 'TRX'},
                'CRO': {'CRC20': 'CRONOS'},
            },
        }

    def safe_ledger_entry(self, entry: object, currency: Optional[object] = None):
        currency = self.safe_currency(None, currency)
        direction = self.safe_string(entry, 'direction')
        before = self.safe_string(entry, 'before')
        after = self.safe_string(entry, 'after')
        amount = self.safe_string(entry, 'amount')
        if amount is not None:
            if before is None and after is not None:
                before = Precise.string_sub(after, amount)
            elif before is not None and after is None:
                after = Precise.string_add(before, amount)
        if before is not None and after is not None:
            if direction is None:
                if Precise.string_gt(before, after):
                    direction = 'out'
                if Precise.string_gt(after, before):
                    direction = 'in'
        fee = self.safe_value(entry, 'fee')
        if fee is not None:
            fee['cost'] = self.safe_number(fee, 'cost')
        timestamp = self.safe_integer(entry, 'timestamp')
        return {
            'id': self.safe_string(entry, 'id'),
            'timestamp': timestamp,
            'datetime': self.iso8601(timestamp),
            'direction': direction,
            'account': self.safe_string(entry, 'account'),
            'referenceId': self.safe_string(entry, 'referenceId'),
            'referenceAccount': self.safe_string(entry, 'referenceAccount'),
            'type': self.safe_string(entry, 'type'),
            'currency': currency['code'],
            'amount': self.parse_number(amount),
            'before': self.parse_number(before),
            'after': self.parse_number(after),
            'status': self.safe_string(entry, 'status'),
            'fee': fee,
            'info': entry,
        }

    def safe_currency_structure(self, currency: object):
        return self.extend({
            'info': None,
            'id': None,
            'numericId': None,
            'code': None,
            'precision': None,
            'type': None,
            'name': None,
            'active': None,
            'deposit': None,
            'withdraw': None,
            'fee': None,
            'fees': {},
            'networks': {},
            'limits': {
                'deposit': {
                    'min': None,
                    'max': None,
                },
                'withdraw': {
                    'min': None,
                    'max': None,
                },
            },
        }, currency)

    def set_markets(self, markets, currencies=None):
        values = []
        self.markets_by_id = {}
        # handle marketId conflicts
        # we insert spot markets first
        marketValues = self.sort_by(self.to_array(markets), 'spot', True)
        for i in range(0, len(marketValues)):
            value = marketValues[i]
            if value['id'] in self.markets_by_id:
                (self.markets_by_id[value['id']]).append(value)
            else:
                self.markets_by_id[value['id']] = [value]
            market = self.deep_extend(self.safe_market(), {
                'precision': self.precision,
                'limits': self.limits,
            }, self.fees['trading'], value)
            values.append(market)
        self.markets = self.index_by(values, 'symbol')
        marketsSortedBySymbol = self.keysort(self.markets)
        marketsSortedById = self.keysort(self.markets_by_id)
        self.symbols = list(marketsSortedBySymbol.keys())
        self.ids = list(marketsSortedById.keys())
        if currencies is not None:
            # currencies is always None when called in constructor but not when called from loadMarkets
            self.currencies = self.deep_extend(self.currencies, currencies)
        else:
            baseCurrencies = []
            quoteCurrencies = []
            for i in range(0, len(values)):
                market = values[i]
                defaultCurrencyPrecision = 8 if (self.precisionMode == DECIMAL_PLACES) else self.parse_number('1e-8')
                marketPrecision = self.safe_value(market, 'precision', {})
                if 'base' in market:
                    currency = self.safe_currency_structure({
                        'id': self.safe_string_2(market, 'baseId', 'base'),
                        'numericId': self.safe_integer(market, 'baseNumericId'),
                        'code': self.safe_string(market, 'base'),
                        'precision': self.safe_value_2(marketPrecision, 'base', 'amount', defaultCurrencyPrecision),
                    })
                    baseCurrencies.append(currency)
                if 'quote' in market:
                    currency = self.safe_currency_structure({
                        'id': self.safe_string_2(market, 'quoteId', 'quote'),
                        'numericId': self.safe_integer(market, 'quoteNumericId'),
                        'code': self.safe_string(market, 'quote'),
                        'precision': self.safe_value_2(marketPrecision, 'quote', 'price', defaultCurrencyPrecision),
                    })
                    quoteCurrencies.append(currency)
            baseCurrencies = self.sort_by(baseCurrencies, 'code')
            quoteCurrencies = self.sort_by(quoteCurrencies, 'code')
            self.baseCurrencies = self.index_by(baseCurrencies, 'code')
            self.quoteCurrencies = self.index_by(quoteCurrencies, 'code')
            allCurrencies = self.array_concat(baseCurrencies, quoteCurrencies)
            groupedCurrencies = self.group_by(allCurrencies, 'code')
            codes = list(groupedCurrencies.keys())
            resultingCurrencies = []
            for i in range(0, len(codes)):
                code = codes[i]
                groupedCurrenciesCode = self.safe_value(groupedCurrencies, code, [])
                highestPrecisionCurrency = self.safe_value(groupedCurrenciesCode, 0)
                for j in range(1, len(groupedCurrenciesCode)):
                    currentCurrency = groupedCurrenciesCode[j]
                    if self.precisionMode == TICK_SIZE:
                        highestPrecisionCurrency = currentCurrency if (currentCurrency['precision'] < highestPrecisionCurrency['precision']) else highestPrecisionCurrency
                    else:
                        highestPrecisionCurrency = currentCurrency if (currentCurrency['precision'] > highestPrecisionCurrency['precision']) else highestPrecisionCurrency
                resultingCurrencies.append(highestPrecisionCurrency)
            sortedCurrencies = self.sort_by(resultingCurrencies, 'code')
            self.currencies = self.deep_extend(self.currencies, self.index_by(sortedCurrencies, 'code'))
        self.currencies_by_id = self.index_by(self.currencies, 'id')
        currenciesSortedByCode = self.keysort(self.currencies)
        self.codes = list(currenciesSortedByCode.keys())
        return self.markets

    def safe_balance(self, balance: object):
        balances = self.omit(balance, ['info', 'timestamp', 'datetime', 'free', 'used', 'total'])
        codes = list(balances.keys())
        balance['free'] = {}
        balance['used'] = {}
        balance['total'] = {}
        debtBalance = {}
        for i in range(0, len(codes)):
            code = codes[i]
            total = self.safe_string(balance[code], 'total')
            free = self.safe_string(balance[code], 'free')
            used = self.safe_string(balance[code], 'used')
            debt = self.safe_string(balance[code], 'debt')
            if (total is None) and (free is not None) and (used is not None):
                total = Precise.string_add(free, used)
            if (free is None) and (total is not None) and (used is not None):
                free = Precise.string_sub(total, used)
            if (used is None) and (total is not None) and (free is not None):
                used = Precise.string_sub(total, free)
            balance[code]['free'] = self.parse_number(free)
            balance[code]['used'] = self.parse_number(used)
            balance[code]['total'] = self.parse_number(total)
            balance['free'][code] = balance[code]['free']
            balance['used'][code] = balance[code]['used']
            balance['total'][code] = balance[code]['total']
            if debt is not None:
                balance[code]['debt'] = self.parse_number(debt)
                debtBalance[code] = balance[code]['debt']
        debtBalanceArray = list(debtBalance.keys())
        length = len(debtBalanceArray)
        if length:
            balance['debt'] = debtBalance
        return balance

    def safe_order(self, order: object, market: Optional[object] = None):
        # parses numbers
        # * it is important pass the trades rawTrades
        amount = self.omit_zero(self.safe_string(order, 'amount'))
        remaining = self.safe_string(order, 'remaining')
        filled = self.safe_string(order, 'filled')
        cost = self.safe_string(order, 'cost')
        average = self.omit_zero(self.safe_string(order, 'average'))
        price = self.omit_zero(self.safe_string(order, 'price'))
        lastTradeTimeTimestamp = self.safe_integer(order, 'lastTradeTimestamp')
        symbol = self.safe_string(order, 'symbol')
        side = self.safe_string(order, 'side')
        status = self.safe_string(order, 'status')
        parseFilled = (filled is None)
        parseCost = (cost is None)
        parseLastTradeTimeTimestamp = (lastTradeTimeTimestamp is None)
        fee = self.safe_value(order, 'fee')
        parseFee = (fee is None)
        parseFees = self.safe_value(order, 'fees') is None
        parseSymbol = symbol is None
        parseSide = side is None
        shouldParseFees = parseFee or parseFees
        fees = self.safe_value(order, 'fees', [])
        trades = []
        if parseFilled or parseCost or shouldParseFees:
            rawTrades = self.safe_value(order, 'trades', trades)
            oldNumber = self.number
            # we parse trades here!
            self.number = str
            firstTrade = self.safe_value(rawTrades, 0)
            # parse trades if they haven't already been parsed
            tradesAreParsed = ((firstTrade is not None) and ('info' in firstTrade) and ('id' in firstTrade))
            if not tradesAreParsed:
                trades = self.parse_trades(rawTrades, market)
            self.number = oldNumber
            tradesLength = 0
            isArray = isinstance(trades, list)
            if isArray:
                tradesLength = len(trades)
            if isArray and (tradesLength > 0):
                # move properties that are defined in trades up into the order
                if order['symbol'] is None:
                    order['symbol'] = trades[0]['symbol']
                if order['side'] is None:
                    order['side'] = trades[0]['side']
                if order['type'] is None:
                    order['type'] = trades[0]['type']
                if order['id'] is None:
                    order['id'] = trades[0]['order']
                if parseFilled:
                    filled = '0'
                if parseCost:
                    cost = '0'
                for i in range(0, len(trades)):
                    trade = trades[i]
                    tradeAmount = self.safe_string(trade, 'amount')
                    if parseFilled and (tradeAmount is not None):
                        filled = Precise.string_add(filled, tradeAmount)
                    tradeCost = self.safe_string(trade, 'cost')
                    if parseCost and (tradeCost is not None):
                        cost = Precise.string_add(cost, tradeCost)
                    if parseSymbol:
                        symbol = self.safe_string(trade, 'symbol')
                    if parseSide:
                        side = self.safe_string(trade, 'side')
                    tradeTimestamp = self.safe_value(trade, 'timestamp')
                    if parseLastTradeTimeTimestamp and (tradeTimestamp is not None):
                        if lastTradeTimeTimestamp is None:
                            lastTradeTimeTimestamp = tradeTimestamp
                        else:
                            lastTradeTimeTimestamp = max(lastTradeTimeTimestamp, tradeTimestamp)
                    if shouldParseFees:
                        tradeFees = self.safe_value(trade, 'fees')
                        if tradeFees is not None:
                            for j in range(0, len(tradeFees)):
                                tradeFee = tradeFees[j]
                                fees.append(self.extend({}, tradeFee))
                        else:
                            tradeFee = self.safe_value(trade, 'fee')
                            if tradeFee is not None:
                                fees.append(self.extend({}, tradeFee))
        if shouldParseFees:
            reducedFees = self.reduce_fees_by_currency(fees) if self.reduceFees else fees
            reducedLength = len(reducedFees)
            for i in range(0, reducedLength):
                reducedFees[i]['cost'] = self.safe_number(reducedFees[i], 'cost')
                if 'rate' in reducedFees[i]:
                    reducedFees[i]['rate'] = self.safe_number(reducedFees[i], 'rate')
            if not parseFee and (reducedLength == 0):
                fee['cost'] = self.safe_number(fee, 'cost')
                if 'rate' in fee:
                    fee['rate'] = self.safe_number(fee, 'rate')
                reducedFees.append(fee)
            order['fees'] = reducedFees
            if parseFee and (reducedLength == 1):
                order['fee'] = reducedFees[0]
        if amount is None:
            # ensure amount = filled + remaining
            if filled is not None and remaining is not None:
                amount = Precise.string_add(filled, remaining)
            elif status == 'closed':
                amount = filled
        if filled is None:
            if amount is not None and remaining is not None:
                filled = Precise.string_sub(amount, remaining)
            elif status == 'closed' and amount is not None:
                filled = amount
        if remaining is None:
            if amount is not None and filled is not None:
                remaining = Precise.string_sub(amount, filled)
            elif status == 'closed':
                remaining = '0'
        # ensure that the average field is calculated correctly
        inverse = self.safe_value(market, 'inverse', False)
        contractSize = self.number_to_string(self.safe_value(market, 'contractSize', 1))
        # inverse
        # price = filled * contract size / cost
        #
        # linear
        # price = cost / (filled * contract size)
        if average is None:
            if (filled is not None) and (cost is not None) and Precise.string_gt(filled, '0'):
                filledTimesContractSize = Precise.string_mul(filled, contractSize)
                if inverse:
                    average = Precise.string_div(filledTimesContractSize, cost)
                else:
                    average = Precise.string_div(cost, filledTimesContractSize)
        # similarly
        # inverse
        # cost = filled * contract size / price
        #
        # linear
        # cost = filled * contract size * price
        costPriceExists = (average is not None) or (price is not None)
        if parseCost and (filled is not None) and costPriceExists:
            multiplyPrice = None
            if average is None:
                multiplyPrice = price
            else:
                multiplyPrice = average
            # contract trading
            filledTimesContractSize = Precise.string_mul(filled, contractSize)
            if inverse:
                cost = Precise.string_div(filledTimesContractSize, multiplyPrice)
            else:
                cost = Precise.string_mul(filledTimesContractSize, multiplyPrice)
        # support for market orders
        orderType = self.safe_value(order, 'type')
        emptyPrice = (price is None) or Precise.string_equals(price, '0')
        if emptyPrice and (orderType == 'market'):
            price = average
        # we have trades with string values at self point so we will mutate them
        for i in range(0, len(trades)):
            entry = trades[i]
            entry['amount'] = self.safe_number(entry, 'amount')
            entry['price'] = self.safe_number(entry, 'price')
            entry['cost'] = self.safe_number(entry, 'cost')
            tradeFee = self.safe_value(entry, 'fee', {})
            tradeFee['cost'] = self.safe_number(tradeFee, 'cost')
            if 'rate' in tradeFee:
                tradeFee['rate'] = self.safe_number(tradeFee, 'rate')
            entry['fee'] = tradeFee
        timeInForce = self.safe_string(order, 'timeInForce')
        postOnly = self.safe_value(order, 'postOnly')
        # timeInForceHandling
        if timeInForce is None:
            if self.safe_string(order, 'type') == 'market':
                timeInForce = 'IOC'
            # allow postOnly override
            if postOnly:
                timeInForce = 'PO'
        elif postOnly is None:
            # timeInForce is not None here
            postOnly = timeInForce == 'PO'
        timestamp = self.safe_integer(order, 'timestamp')
        lastUpdateTimestamp = self.safe_integer(order, 'lastUpdateTimestamp')
        datetime = self.safe_string(order, 'datetime')
        if datetime is None:
            datetime = self.iso8601(timestamp)
        triggerPrice = self.parse_number(self.safe_string_2(order, 'triggerPrice', 'stopPrice'))
        takeProfitPrice = self.parse_number(self.safe_string(order, 'takeProfitPrice'))
        stopLossPrice = self.parse_number(self.safe_string(order, 'stopLossPrice'))
        return self.extend(order, {
            'id': self.safe_string(order, 'id'),
            'clientOrderId': self.safe_string(order, 'clientOrderId'),
            'timestamp': timestamp,
            'datetime': datetime,
            'symbol': symbol,
            'type': self.safe_string(order, 'type'),
            'side': side,
            'lastTradeTimestamp': lastTradeTimeTimestamp,
            'lastUpdateTimestamp': lastUpdateTimestamp,
            'price': self.parse_number(price),
            'amount': self.parse_number(amount),
            'cost': self.parse_number(cost),
            'average': self.parse_number(average),
            'filled': self.parse_number(filled),
            'remaining': self.parse_number(remaining),
            'timeInForce': timeInForce,
            'postOnly': postOnly,
            'trades': trades,
            'reduceOnly': self.safe_value(order, 'reduceOnly'),
            'stopPrice': triggerPrice,  # ! deprecated, use triggerPrice instead
            'triggerPrice': triggerPrice,
            'takeProfitPrice': takeProfitPrice,
            'stopLossPrice': stopLossPrice,
            'status': status,
            'fee': self.safe_value(order, 'fee'),
        })

    def parse_orders(self, orders: object, market: Optional[object] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        #
        # the value of orders is either a dict or a list
        #
        # dict
        #
        #     {
        #         'id1': {...},
        #         'id2': {...},
        #         'id3': {...},
        #         ...
        #     }
        #
        # list
        #
        #     [
        #         {'id': 'id1', ...},
        #         {'id': 'id2', ...},
        #         {'id': 'id3', ...},
        #         ...
        #     ]
        #
        results = []
        if isinstance(orders, list):
            for i in range(0, len(orders)):
                order = self.extend(self.parse_order(orders[i], market), params)
                results.append(order)
        else:
            ids = list(orders.keys())
            for i in range(0, len(ids)):
                id = ids[i]
                order = self.extend(self.parse_order(self.extend({'id': id}, orders[id]), market), params)
                results.append(order)
        results = self.sort_by(results, 'timestamp')
        symbol = market['symbol'] if (market is not None) else None
        return self.filter_by_symbol_since_limit(results, symbol, since, limit)

    def calculate_fee(self, symbol: str, type: str, side: str, amount: float, price: float, takerOrMaker='taker', params={}):
        if type == 'market' and takerOrMaker == 'maker':
            raise ArgumentsRequired(self.id + ' calculateFee() - you have provided incompatible arguments - "market" type order can not be "maker". Change either the "type" or the "takerOrMaker" argument to calculate the fee.')
        market = self.markets[symbol]
        feeSide = self.safe_string(market, 'feeSide', 'quote')
        useQuote = None
        if feeSide == 'get':
            # the fee is always in the currency you get
            useQuote = side == 'sell'
        elif feeSide == 'give':
            # the fee is always in the currency you give
            useQuote = side == 'buy'
        else:
            # the fee is always in feeSide currency
            useQuote = feeSide == 'quote'
        cost = self.number_to_string(amount)
        key = None
        if useQuote:
            priceString = self.number_to_string(price)
            cost = Precise.string_mul(cost, priceString)
            key = 'quote'
        else:
            key = 'base'
        # for derivatives, the fee is in 'settle' currency
        if not market['spot']:
            key = 'settle'
        # even if `takerOrMaker` argument was set to 'maker', for 'market' orders we should forcefully override it to 'taker'
        if type == 'market':
            takerOrMaker = 'taker'
        rate = self.safe_string(market, takerOrMaker)
        cost = Precise.string_mul(cost, rate)
        return {
            'type': takerOrMaker,
            'currency': market[key],
            'rate': self.parse_number(rate),
            'cost': self.parse_number(cost),
        }

    def safe_trade(self, trade: object, market: Optional[object] = None):
        amount = self.safe_string(trade, 'amount')
        price = self.safe_string(trade, 'price')
        cost = self.safe_string(trade, 'cost')
        if cost is None:
            # contract trading
            contractSize = self.safe_string(market, 'contractSize')
            multiplyPrice = price
            if contractSize is not None:
                inverse = self.safe_value(market, 'inverse', False)
                if inverse:
                    multiplyPrice = Precise.string_div('1', price)
                multiplyPrice = Precise.string_mul(multiplyPrice, contractSize)
            cost = Precise.string_mul(multiplyPrice, amount)
        parseFee = self.safe_value(trade, 'fee') is None
        parseFees = self.safe_value(trade, 'fees') is None
        shouldParseFees = parseFee or parseFees
        fees = []
        fee = self.safe_value(trade, 'fee')
        if shouldParseFees:
            reducedFees = self.reduce_fees_by_currency(fees) if self.reduceFees else fees
            reducedLength = len(reducedFees)
            for i in range(0, reducedLength):
                reducedFees[i]['cost'] = self.safe_number(reducedFees[i], 'cost')
                if 'rate' in reducedFees[i]:
                    reducedFees[i]['rate'] = self.safe_number(reducedFees[i], 'rate')
            if not parseFee and (reducedLength == 0):
                fee['cost'] = self.safe_number(fee, 'cost')
                if 'rate' in fee:
                    fee['rate'] = self.safe_number(fee, 'rate')
                reducedFees.append(fee)
            if parseFees:
                trade['fees'] = reducedFees
            if parseFee and (reducedLength == 1):
                trade['fee'] = reducedFees[0]
            tradeFee = self.safe_value(trade, 'fee')
            if tradeFee is not None:
                tradeFee['cost'] = self.safe_number(tradeFee, 'cost')
                if 'rate' in tradeFee:
                    tradeFee['rate'] = self.safe_number(tradeFee, 'rate')
                trade['fee'] = tradeFee
        trade['amount'] = self.parse_number(amount)
        trade['price'] = self.parse_number(price)
        trade['cost'] = self.parse_number(cost)
        return trade

    def invert_flat_string_dictionary(self, dict):
        reversed = {}
        keys = list(dict.keys())
        for i in range(0, len(keys)):
            key = keys[i]
            value = dict[key]
            if isinstance(value, str):
                reversed[value] = key
        return reversed

    def reduce_fees_by_currency(self, fees):
        #
        # self function takes a list of fee structures having the following format
        #
        #     string = True
        #
        #     [
        #         {'currency': 'BTC', 'cost': '0.1'},
        #         {'currency': 'BTC', 'cost': '0.2'  },
        #         {'currency': 'BTC', 'cost': '0.2', 'rate': '0.00123'},
        #         {'currency': 'BTC', 'cost': '0.4', 'rate': '0.00123'},
        #         {'currency': 'BTC', 'cost': '0.5', 'rate': '0.00456'},
        #         {'currency': 'USDT', 'cost': '12.3456'},
        #     ]
        #
        #     string = False
        #
        #     [
        #         {'currency': 'BTC', 'cost': 0.1},
        #         {'currency': 'BTC', 'cost': 0.2},
        #         {'currency': 'BTC', 'cost': 0.2, 'rate': 0.00123},
        #         {'currency': 'BTC', 'cost': 0.4, 'rate': 0.00123},
        #         {'currency': 'BTC', 'cost': 0.5, 'rate': 0.00456},
        #         {'currency': 'USDT', 'cost': 12.3456},
        #     ]
        #
        # and returns a reduced fee list, where fees are summed per currency and rate(if any)
        #
        #     string = True
        #
        #     [
        #         {'currency': 'BTC', 'cost': '0.3'  },
        #         {'currency': 'BTC', 'cost': '0.6', 'rate': '0.00123'},
        #         {'currency': 'BTC', 'cost': '0.5', 'rate': '0.00456'},
        #         {'currency': 'USDT', 'cost': '12.3456'},
        #     ]
        #
        #     string  = False
        #
        #     [
        #         {'currency': 'BTC', 'cost': 0.3  },
        #         {'currency': 'BTC', 'cost': 0.6, 'rate': 0.00123},
        #         {'currency': 'BTC', 'cost': 0.5, 'rate': 0.00456},
        #         {'currency': 'USDT', 'cost': 12.3456},
        #     ]
        #
        reduced = {}
        for i in range(0, len(fees)):
            fee = fees[i]
            feeCurrencyCode = self.safe_string(fee, 'currency')
            if feeCurrencyCode is not None:
                rate = self.safe_string(fee, 'rate')
                cost = self.safe_value(fee, 'cost')
                if Precise.string_eq(cost, '0'):
                    # omit zero cost fees
                    continue
                if not (feeCurrencyCode in reduced):
                    reduced[feeCurrencyCode] = {}
                rateKey = '' if (rate is None) else rate
                if rateKey in reduced[feeCurrencyCode]:
                    reduced[feeCurrencyCode][rateKey]['cost'] = Precise.string_add(reduced[feeCurrencyCode][rateKey]['cost'], cost)
                else:
                    reduced[feeCurrencyCode][rateKey] = {
                        'currency': feeCurrencyCode,
                        'cost': cost,
                    }
                    if rate is not None:
                        reduced[feeCurrencyCode][rateKey]['rate'] = rate
        result = []
        feeValues = list(reduced.values())
        for i in range(0, len(feeValues)):
            reducedFeeValues = list(feeValues[i].values())
            result = self.array_concat(result, reducedFeeValues)
        return result

    def safe_ticker(self, ticker: object, market=None):
        open = self.safe_value(ticker, 'open')
        close = self.safe_value(ticker, 'close')
        last = self.safe_value(ticker, 'last')
        change = self.safe_value(ticker, 'change')
        percentage = self.safe_value(ticker, 'percentage')
        average = self.safe_value(ticker, 'average')
        vwap = self.safe_value(ticker, 'vwap')
        baseVolume = self.safe_string(ticker, 'baseVolume')
        quoteVolume = self.safe_string(ticker, 'quoteVolume')
        if vwap is None:
            vwap = Precise.string_div(quoteVolume, baseVolume)
        if (last is not None) and (close is None):
            close = last
        elif (last is None) and (close is not None):
            last = close
        if (last is not None) and (open is not None):
            if change is None:
                change = Precise.string_sub(last, open)
            if average is None:
                average = Precise.string_div(Precise.string_add(last, open), '2')
        if (percentage is None) and (change is not None) and (open is not None) and Precise.string_gt(open, '0'):
            percentage = Precise.string_mul(Precise.string_div(change, open), '100')
        if (change is None) and (percentage is not None) and (open is not None):
            change = Precise.string_div(Precise.string_mul(percentage, open), '100')
        if (open is None) and (last is not None) and (change is not None):
            open = Precise.string_sub(last, change)
        # timestamp and symbol operations don't belong in safeTicker
        # they should be done in the derived classes
        return self.extend(ticker, {
            'bid': self.omit_zero(self.safe_number(ticker, 'bid')),
            'bidVolume': self.safe_number(ticker, 'bidVolume'),
            'ask': self.omit_zero(self.safe_number(ticker, 'ask')),
            'askVolume': self.safe_number(ticker, 'askVolume'),
            'high': self.omit_zero(self.safe_number(ticker, 'high')),
            'low': self.omit_zero(self.safe_number(ticker, 'low')),
            'open': self.omit_zero(self.parse_number(open)),
            'close': self.omit_zero(self.parse_number(close)),
            'last': self.omit_zero(self.parse_number(last)),
            'change': self.parse_number(change),
            'percentage': self.parse_number(percentage),
            'average': self.omit_zero(self.parse_number(average)),
            'vwap': self.omit_zero(self.parse_number(vwap)),
            'baseVolume': self.parse_number(baseVolume),
            'quoteVolume': self.parse_number(quoteVolume),
            'previousClose': self.safe_number(ticker, 'previousClose'),
        })

    async def fetch_ohlcv(self, symbol: str, timeframe='1m', since: Optional[int] = None, limit: Optional[int] = None, params={}):
        message = ''
        if self.has['fetchTrades']:
            message = '. If you want to build OHLCV candles from trade executions data, visit https://github.com/ccxt/ccxt/tree/master/examples/ and see "build-ohlcv-bars" file'
        raise NotSupported(self.id + ' fetchOHLCV() is not supported yet' + message)

    async def watch_ohlcv(self, symbol: str, timeframe='1m', since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchOHLCV() is not supported yet')

    def convert_trading_view_to_ohlcv(self, ohlcvs, timestamp='t', open='o', high='h', low='l', close='c', volume='v', ms=False):
        result = []
        timestamps = self.safe_value(ohlcvs, timestamp, [])
        opens = self.safe_value(ohlcvs, open, [])
        highs = self.safe_value(ohlcvs, high, [])
        lows = self.safe_value(ohlcvs, low, [])
        closes = self.safe_value(ohlcvs, close, [])
        volumes = self.safe_value(ohlcvs, volume, [])
        for i in range(0, len(timestamps)):
            result.append([
                self.safe_integer(timestamps, i) if ms else self.safe_timestamp(timestamps, i),
                self.safe_value(opens, i),
                self.safe_value(highs, i),
                self.safe_value(lows, i),
                self.safe_value(closes, i),
                self.safe_value(volumes, i),
            ])
        return result

    def convert_ohlcv_to_trading_view(self, ohlcvs, timestamp='t', open='o', high='h', low='l', close='c', volume='v', ms=False):
        result = {}
        result[timestamp] = []
        result[open] = []
        result[high] = []
        result[low] = []
        result[close] = []
        result[volume] = []
        for i in range(0, len(ohlcvs)):
            ts = ohlcvs[i][0] if ms else self.parseToInt(ohlcvs[i][0] / 1000)
            result[timestamp].append(ts)
            result[open].append(ohlcvs[i][1])
            result[high].append(ohlcvs[i][2])
            result[low].append(ohlcvs[i][3])
            result[close].append(ohlcvs[i][4])
            result[volume].append(ohlcvs[i][5])
        return result

    async def fetch_web_endpoint(self, method, endpointMethod, returnAsJson, startRegex=None, endRegex=None):
        errorMessage = ''
        options = self.safe_value(self.options, method, {})
        muteOnFailure = self.safe_value(options, 'webApiMuteFailure', True)
        try:
            # if it was not explicitly disabled, then don't fetch
            if self.safe_value(options, 'webApiEnable', True) is not True:
                return None
            maxRetries = self.safe_value(options, 'webApiRetries', 10)
            response = None
            retry = 0
            while(retry < maxRetries):
                try:
                    response = await getattr(self, endpointMethod)({})
                    break
                except Exception as e:
                    retry = retry + 1
                    if retry == maxRetries:
                        raise e
            content = response
            if startRegex is not None:
                splitted_by_start = content.split(startRegex)
                content = splitted_by_start[1]  # we need second part after start
            if endRegex is not None:
                splitted_by_end = content.split(endRegex)
                content = splitted_by_end[0]  # we need first part after start
            if returnAsJson and (isinstance(content, str)):
                jsoned = self.parse_json(content.strip())  # content should be trimmed before json parsing
                if jsoned:
                    return jsoned  # if parsing was not successfull, exception should be thrown
                else:
                    raise BadResponse('could not parse the response into json')
            else:
                return content
        except Exception as e:
            errorMessage = self.id + ' ' + method + '() failed to fetch correct data from website. Probably webpage markup has been changed, breaking the page custom parser.'
        if muteOnFailure:
            return None
        else:
            raise BadResponse(errorMessage)

    def market_ids(self, symbols):
        if symbols is None:
            return symbols
        result = []
        for i in range(0, len(symbols)):
            result.append(self.market_id(symbols[i]))
        return result

    def market_symbols(self, symbols, type: Optional[str] = None, allowEmpty=True):
        if symbols is None:
            if not allowEmpty:
                raise ArgumentsRequired(self.id + ' empty list of symbols is not supported')
            return symbols
        symbolsLength = len(symbols)
        if symbolsLength == 0:
            if not allowEmpty:
                raise ArgumentsRequired(self.id + ' empty list of symbols is not supported')
            return symbols
        result = []
        for i in range(0, len(symbols)):
            market = self.market(symbols[i])
            if type is not None and market['type'] != type:
                raise BadRequest(self.id + ' symbols must be of same type ' + type + '. If the type is incorrect you can change it in options or the params of the request')
            symbol = self.safe_string(market, 'symbol', symbols[i])
            result.append(symbol)
        return result

    def market_codes(self, codes):
        if codes is None:
            return codes
        result = []
        for i in range(0, len(codes)):
            result.append(self.common_currency_code(codes[i]))
        return result

    def parse_bids_asks(self, bidasks, priceKey: IndexType = 0, amountKey: IndexType = 1):
        bidasks = self.to_array(bidasks)
        result = []
        for i in range(0, len(bidasks)):
            result.append(self.parse_bid_ask(bidasks[i], priceKey, amountKey))
        return result

    async def fetch_l2_order_book(self, symbol: str, limit: Optional[int] = None, params={}):
        orderbook = await self.fetch_order_book(symbol, limit, params)
        return self.extend(orderbook, {
            'asks': self.sort_by(self.aggregate(orderbook['asks']), 0),
            'bids': self.sort_by(self.aggregate(orderbook['bids']), 0, True),
        })

    def filter_by_symbol(self, objects, symbol: Optional[str] = None):
        if symbol is None:
            return objects
        result = []
        for i in range(0, len(objects)):
            objectSymbol = self.safe_string(objects[i], 'symbol')
            if objectSymbol == symbol:
                result.append(objects[i])
        return result

    def parse_ohlcv(self, ohlcv, market=None):
        if isinstance(ohlcv, list):
            return [
                self.safe_integer(ohlcv, 0),  # timestamp
                self.safe_number(ohlcv, 1),  # open
                self.safe_number(ohlcv, 2),  # high
                self.safe_number(ohlcv, 3),  # low
                self.safe_number(ohlcv, 4),  # close
                self.safe_number(ohlcv, 5),  # volume
            ]
        return ohlcv

    def network_code_to_id(self, networkCode, currencyCode=None):
        """
         * @ignore
        tries to convert the provided networkCode(which is expected to be an unified network code) to a network id. In order to achieve self, derived class needs to have 'options->networks' defined.
        :param str networkCode: unified network code
        :param str currencyCode: unified currency code, but self argument is not required by default, unless there is an exchange(like huobi) that needs an override of the method to be able to pass currencyCode argument additionally
        :returns str|None: exchange-specific network id
        """
        networkIdsByCodes = self.safe_value(self.options, 'networks', {})
        networkId = self.safe_string(networkIdsByCodes, networkCode)
        # for example, if 'ETH' is passed for networkCode, but 'ETH' key not defined in `options->networks` object
        if networkId is None:
            if currencyCode is None:
                # if currencyCode was not provided, then we just set passed value to networkId
                networkId = networkCode
            else:
                # if currencyCode was provided, then we try to find if that currencyCode has a replacement(i.e. ERC20 for ETH)
                defaultNetworkCodeReplacements = self.safe_value(self.options, 'defaultNetworkCodeReplacements', {})
                if currencyCode in defaultNetworkCodeReplacements:
                    # if there is a replacement for the passed networkCode, then we use it to find network-id in `options->networks` object
                    replacementObject = defaultNetworkCodeReplacements[currencyCode]  # i.e. {'ERC20': 'ETH'}
                    keys = list(replacementObject.keys())
                    for i in range(0, len(keys)):
                        key = keys[i]
                        value = replacementObject[key]
                        # if value matches to provided unified networkCode, then we use it's key to find network-id in `options->networks` object
                        if value == networkCode:
                            networkId = self.safe_string(networkIdsByCodes, key)
                            break
                # if it wasn't found, we just set the provided value to network-id
                if networkId is None:
                    networkId = networkCode
        return networkId

    def network_id_to_code(self, networkId, currencyCode=None):
        """
         * @ignore
        tries to convert the provided exchange-specific networkId to an unified network Code. In order to achieve self, derived class needs to have "options['networksById']" defined.
        :param str networkId: exchange specific network id/title, like: TRON, Trc-20, usdt-erc20, etc
        :param str|None currencyCode: unified currency code, but self argument is not required by default, unless there is an exchange(like huobi) that needs an override of the method to be able to pass currencyCode argument additionally
        :returns str|None: unified network code
        """
        networkCodesByIds = self.safe_value(self.options, 'networksById', {})
        networkCode = self.safe_string(networkCodesByIds, networkId, networkId)
        # replace mainnet network-codes(i.e. ERC20->ETH)
        if currencyCode is not None:
            defaultNetworkCodeReplacements = self.safe_value(self.options, 'defaultNetworkCodeReplacements', {})
            if currencyCode in defaultNetworkCodeReplacements:
                replacementObject = self.safe_value(defaultNetworkCodeReplacements, currencyCode, {})
                networkCode = self.safe_string(replacementObject, networkCode, networkCode)
        return networkCode

    def handle_network_code_and_params(self, params):
        networkCodeInParams = self.safe_string_2(params, 'networkCode', 'network')
        if networkCodeInParams is not None:
            params = self.omit(params, ['networkCode', 'network'])
        # if it was not defined by user, we should not set it from 'defaultNetworks', because handleNetworkCodeAndParams is for only request-side and thus we do not fill it with anything. We can only use 'defaultNetworks' after parsing response-side
        return [networkCodeInParams, params]

    def default_network_code(self, currencyCode):
        defaultNetworkCode = None
        defaultNetworks = self.safe_value(self.options, 'defaultNetworks', {})
        if currencyCode in defaultNetworks:
            # if currency had set its network in "defaultNetworks", use it
            defaultNetworkCode = defaultNetworks[currencyCode]
        else:
            # otherwise, try to use the global-scope 'defaultNetwork' value(even if that network is not supported by currency, it doesn't make any problem, self will be just used "at first" if currency supports self network at all)
            defaultNetwork = self.safe_value(self.options, 'defaultNetwork')
            if defaultNetwork is not None:
                defaultNetworkCode = defaultNetwork
        return defaultNetworkCode

    def select_network_code_from_unified_networks(self, currencyCode, networkCode, indexedNetworkEntries):
        return self.select_network_key_from_networks(currencyCode, networkCode, indexedNetworkEntries, True)

    def select_network_id_from_raw_networks(self, currencyCode, networkCode, indexedNetworkEntries):
        return self.select_network_key_from_networks(currencyCode, networkCode, indexedNetworkEntries, False)

    def select_network_key_from_networks(self, currencyCode, networkCode, indexedNetworkEntries, isIndexedByUnifiedNetworkCode=False):
        # self method is used against raw & unparse network entries, which are just indexed by network id
        chosenNetworkId = None
        availableNetworkIds = list(indexedNetworkEntries.keys())
        responseNetworksLength = len(availableNetworkIds)
        if networkCode is not None:
            if responseNetworksLength == 0:
                raise NotSupported(self.id + ' - ' + networkCode + ' network did not return any result for ' + currencyCode)
            else:
                # if networkCode was provided by user, we should check it after response, referenced exchange doesn't support network-code during request
                networkId = networkCode if isIndexedByUnifiedNetworkCode else self.network_code_to_id(networkCode, currencyCode)
                if networkId in indexedNetworkEntries:
                    chosenNetworkId = networkId
                else:
                    raise NotSupported(self.id + ' - ' + networkId + ' network was not found for ' + currencyCode + ', use one of ' + ', '.join(availableNetworkIds))
        else:
            if responseNetworksLength == 0:
                raise NotSupported(self.id + ' - no networks were returned for ' + currencyCode)
            else:
                # if networkCode was not provided by user, then we try to use the default network(if it was defined in "defaultNetworks"), otherwise, we just return the first network entry
                defaultNetworkCode = self.default_network_code(currencyCode)
                defaultNetworkId = defaultNetworkCode if isIndexedByUnifiedNetworkCode else self.network_code_to_id(defaultNetworkCode, currencyCode)
                chosenNetworkId = defaultNetworkId if (defaultNetworkId in indexedNetworkEntries) else availableNetworkIds[0]
        return chosenNetworkId

    def safe_number_2(self, dictionary, key1, key2, d=None):
        value = self.safe_string_2(dictionary, key1, key2)
        return self.parse_number(value, d)

    def parse_order_book(self, orderbook: object, symbol: str, timestamp: Optional[float] = None, bidsKey='bids', asksKey='asks', priceKey: IndexType = 0, amountKey: IndexType = 1):
        bids = self.parse_bids_asks(self.safe_value(orderbook, bidsKey, []), priceKey, amountKey)
        asks = self.parse_bids_asks(self.safe_value(orderbook, asksKey, []), priceKey, amountKey)
        return {
            'symbol': symbol,
            'bids': self.sort_by(bids, 0, True),
            'asks': self.sort_by(asks, 0),
            'timestamp': timestamp,
            'datetime': self.iso8601(timestamp),
            'nonce': None,
        }

    def parse_ohlcvs(self, ohlcvs: List[object], market: Optional[Any] = None, timeframe: str = '1m', since: Optional[int] = None, limit: Optional[int] = None):
        results = []
        for i in range(0, len(ohlcvs)):
            results.append(self.parse_ohlcv(ohlcvs[i], market))
        sorted = self.sort_by(results, 0)
        return self.filter_by_since_limit(sorted, since, limit, 0)

    def parse_leverage_tiers(self, response, symbols: Optional[List[str]] = None, marketIdKey=None):
        # marketIdKey should only be None when response is a dictionary
        symbols = self.market_symbols(symbols)
        tiers = {}
        for i in range(0, len(response)):
            item = response[i]
            id = self.safe_string(item, marketIdKey)
            market = self.safe_market(id, None, None, self.safe_string(self.options, 'defaultType'))
            symbol = market['symbol']
            contract = self.safe_value(market, 'contract', False)
            if contract and ((symbols is None) or self.in_array(symbol, symbols)):
                tiers[symbol] = self.parse_market_leverage_tiers(item, market)
        return tiers

    async def load_trading_limits(self, symbols: Optional[List[str]] = None, reload=False, params={}):
        if self.has['fetchTradingLimits']:
            if reload or not ('limitsLoaded' in self.options):
                response = await self.fetch_trading_limits(symbols)
                for i in range(0, len(symbols)):
                    symbol = symbols[i]
                    self.markets[symbol] = self.deep_extend(self.markets[symbol], response[symbol])
                self.options['limitsLoaded'] = self.milliseconds()
        return self.markets

    def safe_position(self, position):
        # simplified version of: /pull/12765/
        unrealizedPnlString = self.safe_string(position, 'unrealisedPnl')
        initialMarginString = self.safe_string(position, 'initialMargin')
        #
        # PERCENTAGE
        #
        percentage = self.safe_value(position, 'percentage')
        if (percentage is None) and (unrealizedPnlString is not None) and (initialMarginString is not None):
            # was done in all implementations( aax, btcex, bybit, deribit, ftx, gate, kucoinfutures, phemex )
            percentageString = Precise.string_mul(Precise.string_div(unrealizedPnlString, initialMarginString, 4), '100')
            position['percentage'] = self.parse_number(percentageString)
        # if contractSize is None get from market
        contractSize = self.safe_number(position, 'contractSize')
        symbol = self.safe_string(position, 'symbol')
        market = None
        if symbol is not None:
            market = self.market(symbol)
        if contractSize is None and market is not None:
            contractSize = self.safe_number(market, 'contractSize')
            position['contractSize'] = contractSize
        return position

    def parse_positions(self, positions, symbols: Optional[List[str]] = None, params={}):
        symbols = self.market_symbols(symbols)
        positions = self.to_array(positions)
        result = []
        for i in range(0, len(positions)):
            position = self.extend(self.parse_position(positions[i], None), params)
            result.append(position)
        return self.filter_by_array_positions(result, 'symbol', symbols, False)

    def parse_accounts(self, accounts, params={}):
        accounts = self.to_array(accounts)
        result = []
        for i in range(0, len(accounts)):
            account = self.extend(self.parse_account(accounts[i]), params)
            result.append(account)
        return result

    def parse_trades(self, trades, market: Optional[object] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        trades = self.to_array(trades)
        result = []
        for i in range(0, len(trades)):
            trade = self.extend(self.parse_trade(trades[i], market), params)
            result.append(trade)
        result = self.sort_by_2(result, 'timestamp', 'id')
        symbol = market['symbol'] if (market is not None) else None
        return self.filter_by_symbol_since_limit(result, symbol, since, limit)

    def parse_transactions(self, transactions, currency: Optional[object] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        transactions = self.to_array(transactions)
        result = []
        for i in range(0, len(transactions)):
            transaction = self.extend(self.parse_transaction(transactions[i], currency), params)
            result.append(transaction)
        result = self.sort_by(result, 'timestamp')
        code = currency['code'] if (currency is not None) else None
        return self.filter_by_currency_since_limit(result, code, since, limit)

    def parse_transfers(self, transfers, currency: Optional[object] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        transfers = self.to_array(transfers)
        result = []
        for i in range(0, len(transfers)):
            transfer = self.extend(self.parse_transfer(transfers[i], currency), params)
            result.append(transfer)
        result = self.sort_by(result, 'timestamp')
        code = currency['code'] if (currency is not None) else None
        return self.filter_by_currency_since_limit(result, code, since, limit)

    def parse_ledger(self, data, currency: Optional[object] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        result = []
        arrayData = self.to_array(data)
        for i in range(0, len(arrayData)):
            itemOrItems = self.parse_ledger_entry(arrayData[i], currency)
            if isinstance(itemOrItems, list):
                for j in range(0, len(itemOrItems)):
                    result.append(self.extend(itemOrItems[j], params))
            else:
                result.append(self.extend(itemOrItems, params))
        result = self.sort_by(result, 'timestamp')
        code = currency['code'] if (currency is not None) else None
        return self.filter_by_currency_since_limit(result, code, since, limit)

    def nonce(self):
        return self.seconds()

    def set_headers(self, headers):
        return headers

    def market_id(self, symbol: str):
        market = self.market(symbol)
        if market is not None:
            return market['id']
        return symbol

    def symbol(self, symbol: str):
        market = self.market(symbol)
        return self.safe_string(market, 'symbol', symbol)

    def resolve_path(self, path, params):
        return [
            self.implode_params(path, params),
            self.omit(params, self.extract_params(path)),
        ]

    def filter_by_array(self, objects, key: IndexType, values=None, indexed=True):
        objects = self.to_array(objects)
        # return all of them if no values were passed
        if values is None or not values:
            return self.index_by(objects, key) if indexed else objects
        results = []
        for i in range(0, len(objects)):
            if self.in_array(objects[i][key], values):
                results.append(objects[i])
        return self.index_by(results, key) if indexed else results

    async def fetch2(self, path, api: Any = 'public', method='GET', params={}, headers: Optional[Any] = None, body: Optional[Any] = None, config={}):
        if self.enableRateLimit:
            cost = self.calculate_rate_limiter_cost(api, method, path, params, config)
            await self.throttle(cost)
        self.lastRestRequestTimestamp = self.milliseconds()
        request = self.sign(path, api, method, params, headers, body)
        return await self.fetch(request['url'], request['method'], request['headers'], request['body'])

    async def request(self, path, api: Any = 'public', method='GET', params={}, headers: Optional[Any] = None, body: Optional[Any] = None, config={}):
        return await self.fetch2(path, api, method, params, headers, body, config)

    async def load_accounts(self, reload=False, params={}):
        if reload:
            self.accounts = await self.fetch_accounts(params)
        else:
            if self.accounts:
                return self.accounts
            else:
                self.accounts = await self.fetch_accounts(params)
        self.accountsById = self.index_by(self.accounts, 'id')
        return self.accounts

    def build_ohlcvc(self, trades: List[Trade], timeframe: str = '1m', since: float = 0, limit: float = 2147483647):
        # given a sorted arrays of trades(recent last) and a timeframe builds an array of OHLCV candles
        # note, default limit value(2147483647) is max int32 value
        ms = self.parse_timeframe(timeframe) * 1000
        ohlcvs = []
        i_timestamp = 0
        # open = 1
        i_high = 2
        i_low = 3
        i_close = 4
        i_volume = 5
        i_count = 6
        tradesLength = len(trades)
        oldest = min(tradesLength, limit)
        for i in range(0, oldest):
            trade = trades[i]
            ts = trade['timestamp']
            if ts < since:
                continue
            openingTime = int(math.floor(ts / ms)) * ms  # shift to the edge of m/h/d(but not M)
            if openingTime < since:  # we don't need bars, that have opening time earlier than requested
                continue
            ohlcv_length = len(ohlcvs)
            candle = ohlcv_length - 1
            if (candle == -1) or (openingTime >= self.sum(ohlcvs[candle][i_timestamp], ms)):
                # moved to a new timeframe -> create a new candle from opening trade
                ohlcvs.append([
                    openingTime,  # timestamp
                    trade['price'],  # O
                    trade['price'],  # H
                    trade['price'],  # L
                    trade['price'],  # C
                    trade['amount'],  # V
                    1,  # count
                ])
            else:
                # still processing the same timeframe -> update opening trade
                ohlcvs[candle][i_high] = max(ohlcvs[candle][i_high], trade['price'])
                ohlcvs[candle][i_low] = min(ohlcvs[candle][i_low], trade['price'])
                ohlcvs[candle][i_close] = trade['price']
                ohlcvs[candle][i_volume] = self.sum(ohlcvs[candle][i_volume], trade['amount'])
                ohlcvs[candle][i_count] = self.sum(ohlcvs[candle][i_count], 1)
        return ohlcvs

    def parse_trading_view_ohlcv(self, ohlcvs, market=None, timeframe='1m', since: Optional[int] = None, limit: Optional[int] = None):
        result = self.convert_trading_view_to_ohlcv(ohlcvs)
        return self.parse_ohlcvs(result, market, timeframe, since, limit)

    async def edit_limit_buy_order(self, id, symbol, amount, price=None, params={}):
        return await self.edit_limit_order(id, symbol, 'buy', amount, price, params)

    async def edit_limit_sell_order(self, id, symbol, amount, price=None, params={}):
        return await self.edit_limit_order(id, symbol, 'sell', amount, price, params)

    async def edit_limit_order(self, id, symbol, side, amount, price=None, params={}):
        return await self.edit_order(id, symbol, 'limit', side, amount, price, params)

    async def edit_order(self, id: str, symbol, type, side, amount=None, price=None, params={}):
        await self.cancelOrder(id, symbol)
        return await self.create_order(symbol, type, side, amount, price, params)

    async def edit_order_ws(self, id: str, symbol: str, type: OrderType, side: OrderSide, amount: float, price: Optional[float] = None, params={}):
        await self.cancelOrderWs(id, symbol)
        return await self.createOrderWs(symbol, type, side, amount, price, params)

    async def fetch_permissions(self, params={}):
        raise NotSupported(self.id + ' fetchPermissions() is not supported yet')

    async def fetch_position(self, symbol: str, params={}):
        raise NotSupported(self.id + ' fetchPosition() is not supported yet')

    async def fetch_positions_by_symbol(self, symbol: str, params={}):
        """
        specifically fetches positions for specific symbol, unlike fetchPositions(which can work with multiple symbols, but because of that, it might be slower & more rate-limit consuming)
        :param str symbol: unified market symbol of the market the position is held in
        :param dict params: extra parameters specific to the endpoint
        :returns dict[]: a list of `position structure <https://github.com/ccxt/ccxt/wiki/Manual#position-structure>` with maximum 3 items - one position for "one-way" mode, and two positions(long & short) for "two-way"(a.k.a. hedge) mode
        """
        raise NotSupported(self.id + ' fetchPositionsBySymbol() is not supported yet')

    async def fetch_positions(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchPositions() is not supported yet')

    async def fetch_positions_risk(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchPositionsRisk() is not supported yet')

    async def fetch_bids_asks(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchBidsAsks() is not supported yet')

    def parse_bid_ask(self, bidask, priceKey: IndexType = 0, amountKey: IndexType = 1):
        price = self.safe_number(bidask, priceKey)
        amount = self.safe_number(bidask, amountKey)
        return [price, amount]

    def safe_currency(self, currencyId: Optional[str], currency: Optional[Any] = None):
        if (currencyId is None) and (currency is not None):
            return currency
        if (self.currencies_by_id is not None) and (currencyId in self.currencies_by_id) and (self.currencies_by_id[currencyId] is not None):
            return self.currencies_by_id[currencyId]
        code = currencyId
        if currencyId is not None:
            code = self.common_currency_code(currencyId.upper())
        return {
            'id': currencyId,
            'code': code,
        }

    def safe_market(self, marketId=None, market=None, delimiter=None, marketType=None):
        result = {
            'id': marketId,
            'symbol': marketId,
            'base': None,
            'quote': None,
            'baseId': None,
            'quoteId': None,
            'active': None,
            'type': None,
            'linear': None,
            'inverse': None,
            'spot': False,
            'swap': False,
            'future': False,
            'option': False,
            'margin': False,
            'contract': False,
            'contractSize': None,
            'expiry': None,
            'expiryDatetime': None,
            'optionType': None,
            'strike': None,
            'settle': None,
            'settleId': None,
            'precision': {
                'amount': None,
                'price': None,
            },
            'limits': {
                'amount': {
                    'min': None,
                    'max': None,
                },
                'price': {
                    'min': None,
                    'max': None,
                },
                'cost': {
                    'min': None,
                    'max': None,
                },
            },
            'info': None,
        }
        if marketId is not None:
            if (self.markets_by_id is not None) and (marketId in self.markets_by_id):
                markets = self.markets_by_id[marketId]
                numMarkets = len(markets)
                if numMarkets == 1:
                    return markets[0]
                else:
                    if (marketType is None) and (market is None):
                        raise ArgumentsRequired(self.id + ' safeMarket() requires a fourth argument for ' + marketId + ' to disambiguate between different markets with the same market id')
                    inferredMarketType = market['type'] if (marketType is None) else marketType
                    for i in range(0, len(markets)):
                        currentMarket = markets[i]
                        if currentMarket[inferredMarketType]:
                            return currentMarket
            elif delimiter is not None:
                parts = marketId.split(delimiter)
                partsLength = len(parts)
                if partsLength == 2:
                    result['baseId'] = self.safe_string(parts, 0)
                    result['quoteId'] = self.safe_string(parts, 1)
                    result['base'] = self.safe_currency_code(result['baseId'])
                    result['quote'] = self.safe_currency_code(result['quoteId'])
                    result['symbol'] = result['base'] + '/' + result['quote']
                    return result
                else:
                    return result
        if market is not None:
            return market
        return result

    def check_required_credentials(self, error=True):
        keys = list(self.requiredCredentials.keys())
        for i in range(0, len(keys)):
            key = keys[i]
            if self.requiredCredentials[key] and not getattr(self, key):
                if error:
                    raise AuthenticationError(self.id + ' requires "' + key + '" credential')
                else:
                    return False
        return True

    def oath(self):
        if self.twofa is not None:
            return self.totp(self.twofa)
        else:
            raise ExchangeError(self.id + ' exchange.twofa has not been set for 2FA Two-Factor Authentication')

    async def fetch_balance(self, params={}):
        raise NotSupported(self.id + ' fetchBalance() is not supported yet')

    async def fetch_balance_ws(self, params={}):
        raise NotSupported(self.id + ' fetchBalanceWs() is not supported yet')

    def parse_balance(self, response):
        raise NotSupported(self.id + ' parseBalance() is not supported yet')

    async def watch_balance(self, params={}):
        raise NotSupported(self.id + ' watchBalance() is not supported yet')

    async def fetch_partial_balance(self, part, params={}):
        balance = await self.fetch_balance(params)
        return balance[part]

    async def fetch_free_balance(self, params={}):
        return await self.fetch_partial_balance('free', params)

    async def fetch_used_balance(self, params={}):
        return await self.fetch_partial_balance('used', params)

    async def fetch_total_balance(self, params={}):
        return await self.fetch_partial_balance('total', params)

    async def fetch_status(self, params={}):
        if self.has['fetchTime']:
            time = await self.fetch_time(params)
            self.status = self.extend(self.status, {
                'updated': time,
                'info': time,
            })
        if not ('info' in self.status):
            self.status['info'] = None
        return self.status

    async def fetch_funding_fee(self, code: str, params={}):
        warnOnFetchFundingFee = self.safe_value(self.options, 'warnOnFetchFundingFee', True)
        if warnOnFetchFundingFee:
            raise NotSupported(self.id + ' fetchFundingFee() method is deprecated, it will be removed in July 2022, please, use fetchTransactionFee() or set exchange.options["warnOnFetchFundingFee"] = False to suppress self warning')
        return await self.fetch_transaction_fee(code, params)

    async def fetch_funding_fees(self, codes: Optional[List[str]] = None, params={}):
        warnOnFetchFundingFees = self.safe_value(self.options, 'warnOnFetchFundingFees', True)
        if warnOnFetchFundingFees:
            raise NotSupported(self.id + ' fetchFundingFees() method is deprecated, it will be removed in July 2022. Please, use fetchTransactionFees() or set exchange.options["warnOnFetchFundingFees"] = False to suppress self warning')
        return await self.fetch_transaction_fees(codes, params)

    async def fetch_transaction_fee(self, code: str, params={}):
        if not self.has['fetchTransactionFees']:
            raise NotSupported(self.id + ' fetchTransactionFee() is not supported yet')
        return await self.fetch_transaction_fees([code], params)

    async def fetch_transaction_fees(self, codes: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchTransactionFees() is not supported yet')

    async def fetch_deposit_withdraw_fees(self, codes: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchDepositWithdrawFees() is not supported yet')

    async def fetch_deposit_withdraw_fee(self, code: str, params={}):
        if not self.has['fetchDepositWithdrawFees']:
            raise NotSupported(self.id + ' fetchDepositWithdrawFee() is not supported yet')
        fees = await self.fetchDepositWithdrawFees([code], params)
        return self.safe_value(fees, code)

    def get_supported_mapping(self, key, mapping={}):
        if key in mapping:
            return mapping[key]
        else:
            raise NotSupported(self.id + ' ' + key + ' does not have a value in mapping')

    async def fetch_borrow_rate(self, code: str, params={}):
        await self.load_markets()
        if not self.has['fetchBorrowRates']:
            raise NotSupported(self.id + ' fetchBorrowRate() is not supported yet')
        borrowRates = await self.fetch_borrow_rates(params)
        rate = self.safe_value(borrowRates, code)
        if rate is None:
            raise ExchangeError(self.id + ' fetchBorrowRate() could not find the borrow rate for currency code ' + code)
        return rate

    def handle_option_and_params(self, params, methodName, optionName, defaultValue=None):
        # This method can be used to obtain method specific properties, i.e: self.handle_option_and_params(params, 'fetchPosition', 'marginMode', 'isolated')
        defaultOptionName = 'default' + self.capitalize(optionName)  # we also need to check the 'defaultXyzWhatever'
        # check if params contain the key
        value = self.safe_value_2(params, optionName, defaultOptionName)
        if value is not None:
            params = self.omit(params, [optionName, defaultOptionName])
        else:
            # check if exchange has properties for self method
            exchangeWideMethodOptions = self.safe_value(self.options, methodName)
            if exchangeWideMethodOptions is not None:
                # check if the option is defined inside self method's props
                value = self.safe_value_2(exchangeWideMethodOptions, optionName, defaultOptionName)
            if value is None:
                # if it's still None, check if global exchange-wide option exists
                value = self.safe_value_2(self.options, optionName, defaultOptionName)
            # if it's still None, use the default value
            value = value if (value is not None) else defaultValue
        return [value, params]

    def handle_option(self, methodName, optionName, defaultValue=None):
        # eslint-disable-next-line no-unused-vars
        result, empty = self.handle_option_and_params({}, methodName, optionName, defaultValue)
        return result

    def handle_market_type_and_params(self, methodName, market=None, params={}):
        defaultType = self.safe_string_2(self.options, 'defaultType', 'type', 'spot')
        methodOptions = self.safe_value(self.options, methodName)
        methodType = defaultType
        if methodOptions is not None:
            if isinstance(methodOptions, str):
                methodType = methodOptions
            else:
                methodType = self.safe_string_2(methodOptions, 'defaultType', 'type', methodType)
        marketType = methodType if (market is None) else market['type']
        type = self.safe_string_2(params, 'defaultType', 'type', marketType)
        params = self.omit(params, ['defaultType', 'type'])
        return [type, params]

    def handle_sub_type_and_params(self, methodName, market=None, params={}, defaultValue=None):
        subType = None
        # if set in params, it takes precedence
        subTypeInParams = self.safe_string_2(params, 'subType', 'defaultSubType')
        # avoid omitting if it's not present
        if subTypeInParams is not None:
            subType = subTypeInParams
            params = self.omit(params, ['subType', 'defaultSubType'])
        else:
            # at first, check from market object
            if market is not None:
                if market['linear']:
                    subType = 'linear'
                elif market['inverse']:
                    subType = 'inverse'
            # if it was not defined in market object
            if subType is None:
                values = self.handle_option_and_params(None, methodName, 'subType', defaultValue)  # no need to re-test params here
                subType = values[0]
        return [subType, params]

    def handle_margin_mode_and_params(self, methodName, params={}, defaultValue=None):
        """
         * @ignore
        :param dict [params]: extra parameters specific to the exchange api endpoint
        :returns Array: the marginMode in lowercase by params["marginMode"], params["defaultMarginMode"] self.options["marginMode"] or self.options["defaultMarginMode"]
        """
        return self.handle_option_and_params(params, methodName, 'marginMode', defaultValue)

    def throw_exactly_matched_exception(self, exact, string, message):
        if string in exact:
            raise exact[string](message)

    def throw_broadly_matched_exception(self, broad, string, message):
        broadKey = self.find_broadly_matched_key(broad, string)
        if broadKey is not None:
            raise broad[broadKey](message)

    def find_broadly_matched_key(self, broad, string):
        # a helper for matching error strings exactly vs broadly
        keys = list(broad.keys())
        for i in range(0, len(keys)):
            key = keys[i]
            if string is not None:  # #issues/12698
                if string.find(key) >= 0:
                    return key
        return None

    def handle_errors(self, statusCode, statusText, url, method, responseHeaders, responseBody, response, requestHeaders, requestBody):
        # it is a stub method that must be overrided in the derived exchange classes
        # raise NotSupported(self.id + ' handleErrors() not implemented yet')
        return None

    def calculate_rate_limiter_cost(self, api, method, path, params, config={}):
        return self.safe_value(config, 'cost', 1)

    async def fetch_ticker(self, symbol: str, params={}):
        if self.has['fetchTickers']:
            await self.load_markets()
            market = self.market(symbol)
            symbol = market['symbol']
            tickers = await self.fetch_tickers([symbol], params)
            ticker = self.safe_value(tickers, symbol)
            if ticker is None:
                raise NullResponse(self.id + ' fetchTickers() could not find a ticker for ' + symbol)
            else:
                return ticker
        else:
            raise NotSupported(self.id + ' fetchTicker() is not supported yet')

    async def watch_ticker(self, symbol: str, params={}):
        raise NotSupported(self.id + ' watchTicker() is not supported yet')

    async def fetch_tickers(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchTickers() is not supported yet')

    async def watch_tickers(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' watchTickers() is not supported yet')

    async def fetch_order(self, id: str, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' fetchOrder() is not supported yet')

    async def fetch_order_ws(self, id: str, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' fetchOrderWs() is not supported yet')

    async def fetch_order_status(self, id: str, symbol: Optional[str] = None, params={}):
        # TODO: TypeScript: change method signature by replacing
        # Promise<string> with Promise<Order['status']>.
        order = await self.fetch_order(id, symbol, params)
        return order['status']

    async def fetch_unified_order(self, order, params={}):
        return await self.fetch_order(self.safe_value(order, 'id'), self.safe_value(order, 'symbol'), params)

    async def create_order(self, symbol: str, type: OrderType, side: OrderSide, amount, price=None, params={}):
        raise NotSupported(self.id + ' createOrder() is not supported yet')

    async def create_order_ws(self, symbol: str, type: OrderType, side: OrderSide, amount: float, price: Optional[float] = None, params={}):
        raise NotSupported(self.id + ' createOrderWs() is not supported yet')

    async def cancel_order(self, id: str, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' cancelOrder() is not supported yet')

    async def cancel_order_ws(self, id: str, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' cancelOrderWs() is not supported yet')

    async def cancel_orders_ws(self, ids: List[str], symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' cancelOrdersWs() is not supported yet')

    async def cancel_all_orders(self, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' cancelAllOrders() is not supported yet')

    async def cancel_all_orders_ws(self, symbol: Optional[str] = None, params={}):
        raise NotSupported(self.id + ' cancelAllOrdersWs() is not supported yet')

    async def cancel_unified_order(self, order, params={}):
        return self.cancelOrder(self.safe_value(order, 'id'), self.safe_value(order, 'symbol'), params)

    async def fetch_orders(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchOrders() is not supported yet')

    async def fetch_order_trades(self, id: str, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchOrderTrades() is not supported yet')

    async def watch_orders(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchOrders() is not supported yet')

    async def fetch_open_orders(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchOpenOrders() is not supported yet')

    async def fetch_open_orders_ws(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchOpenOrdersWs() is not supported yet')

    async def fetch_closed_orders(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchClosedOrders() is not supported yet')

    async def fetch_my_trades(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchMyTrades() is not supported yet')

    async def fetch_my_trades_ws(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchMyTradesWs() is not supported yet')

    async def watch_my_trades(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' watchMyTrades() is not supported yet')

    async def fetch_ohlcv_ws(self, symbol: str, timeframe: str = '1m', since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchOHLCVWs() is not supported yet')

    async def fetch_deposits_withdrawals(self, code: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        """
        fetch history of deposits and withdrawals
        :param str [code]: unified currency code for the currency of the deposit/withdrawals, default is None
        :param int [since]: timestamp in ms of the earliest deposit/withdrawal, default is None
        :param int [limit]: max number of deposit/withdrawals to return, default is None
        :param dict [params]: extra parameters specific to the exchange api endpoint
        :returns dict: a list of `transaction structures <https://github.com/ccxt/ccxt/wiki/Manual#transaction-structure>`
        """
        raise NotSupported(self.id + ' fetchDepositsWithdrawals() is not supported yet')

    async def fetch_deposits(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchDeposits() is not supported yet')

    async def fetch_withdrawals(self, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        raise NotSupported(self.id + ' fetchWithdrawals() is not supported yet')

    async def fetch_open_interest(self, symbol: str, params={}):
        raise NotSupported(self.id + ' fetchOpenInterest() is not supported yet')

    def parse_last_price(self, price, market=None):
        raise NotSupported(self.id + ' parseLastPrice() is not supported yet')

    async def fetch_deposit_address(self, code: str, params={}):
        if self.has['fetchDepositAddresses']:
            depositAddresses = await self.fetchDepositAddresses([code], params)
            depositAddress = self.safe_value(depositAddresses, code)
            if depositAddress is None:
                raise InvalidAddress(self.id + ' fetchDepositAddress() could not find a deposit address for ' + code + ', make sure you have created a corresponding deposit address in your wallet on the exchange website')
            else:
                return depositAddress
        else:
            raise NotSupported(self.id + ' fetchDepositAddress() is not supported yet')

    def account(self) -> Balance:
        return {
            'free': None,
            'used': None,
            'total': None,
        }

    def common_currency_code(self, currency: str):
        if not self.substituteCommonCurrencyCodes:
            return currency
        return self.safe_string(self.commonCurrencies, currency, currency)

    def currency(self, code):
        if self.currencies is None:
            raise ExchangeError(self.id + ' currencies not loaded')
        if isinstance(code, str):
            if code in self.currencies:
                return self.currencies[code]
            elif code in self.currencies_by_id:
                return self.currencies_by_id[code]
        raise ExchangeError(self.id + ' does not have currency code ' + code)

    def market(self, symbol: str):
        if self.markets is None:
            raise ExchangeError(self.id + ' markets not loaded')
        if isinstance(symbol, str):
            if symbol in self.markets:
                return self.markets[symbol]
            elif symbol in self.markets_by_id:
                markets = self.markets_by_id[symbol]
                defaultType = self.safe_string_2(self.options, 'defaultType', 'defaultSubType', 'spot')
                for i in range(0, len(markets)):
                    market = markets[i]
                    if market[defaultType]:
                        return market
                return markets[0]
        raise BadSymbol(self.id + ' does not have market symbol ' + symbol)

    def handle_withdraw_tag_and_params(self, tag, params):
        if isinstance(tag, dict):
            params = self.extend(tag, params)
            tag = None
        if tag is None:
            tag = self.safe_string(params, 'tag')
            if tag is not None:
                params = self.omit(params, 'tag')
        return [tag, params]

    async def create_limit_order(self, symbol: str, side: OrderSide, amount, price, params={}):
        return await self.create_order(symbol, 'limit', side, amount, price, params)

    async def create_market_order(self, symbol: str, side: OrderSide, amount, price=None, params={}):
        return await self.create_order(symbol, 'market', side, amount, price, params)

    async def create_limit_buy_order(self, symbol: str, amount, price, params={}):
        return await self.create_order(symbol, 'limit', 'buy', amount, price, params)

    async def create_limit_sell_order(self, symbol: str, amount, price, params={}):
        return await self.create_order(symbol, 'limit', 'sell', amount, price, params)

    async def create_market_buy_order(self, symbol: str, amount, params={}):
        return await self.create_order(symbol, 'market', 'buy', amount, None, params)

    async def create_market_sell_order(self, symbol: str, amount, params={}):
        return await self.create_order(symbol, 'market', 'sell', amount, None, params)

    def cost_to_precision(self, symbol: str, cost):
        market = self.market(symbol)
        return self.decimal_to_precision(cost, TRUNCATE, market['precision']['price'], self.precisionMode, self.paddingMode)

    def price_to_precision(self, symbol: str, price):
        market = self.market(symbol)
        result = self.decimal_to_precision(price, ROUND, market['precision']['price'], self.precisionMode, self.paddingMode)
        if result == '0':
            raise InvalidOrder(self.id + ' price of ' + market['symbol'] + ' must be greater than minimum price precision of ' + self.number_to_string(market['precision']['price']))
        return result

    def amount_to_precision(self, symbol: str, amount):
        market = self.market(symbol)
        result = self.decimal_to_precision(amount, TRUNCATE, market['precision']['amount'], self.precisionMode, self.paddingMode)
        if result == '0':
            raise InvalidOrder(self.id + ' amount of ' + market['symbol'] + ' must be greater than minimum amount precision of ' + self.number_to_string(market['precision']['amount']))
        return result

    def fee_to_precision(self, symbol: str, fee):
        market = self.market(symbol)
        return self.decimal_to_precision(fee, ROUND, market['precision']['price'], self.precisionMode, self.paddingMode)

    def currency_to_precision(self, code: str, fee, networkCode=None):
        currency = self.currencies[code]
        precision = self.safe_value(currency, 'precision')
        if networkCode is not None:
            networks = self.safe_value(currency, 'networks', {})
            networkItem = self.safe_value(networks, networkCode, {})
            precision = self.safe_value(networkItem, 'precision', precision)
        if precision is None:
            return self.forceString(fee)
        else:
            return self.decimal_to_precision(fee, ROUND, precision, self.precisionMode, self.paddingMode)

    def force_string(self, value):
        if not isinstance(value, str):
            return self.number_to_string(value)
        return value

    def is_tick_precision(self):
        return self.precisionMode == TICK_SIZE

    def is_decimal_precision(self):
        return self.precisionMode == DECIMAL_PLACES

    def is_significant_precision(self):
        return self.precisionMode == SIGNIFICANT_DIGITS

    def safe_number(self, obj: object, key: IndexType, defaultNumber: Optional[float] = None):
        value = self.safe_string(obj, key)
        return self.parse_number(value, defaultNumber)

    def safe_number_n(self, obj: object, arr: List[IndexType], defaultNumber: Optional[float] = None):
        value = self.safe_string_n(obj, arr)
        return self.parse_number(value, defaultNumber)

    def parse_precision(self, precision: Optional[str]):
        """
         * @ignore
        :param str precision: The number of digits to the right of the decimal
        :returns str: a string number equal to 1e-precision
        """
        if precision is None:
            return None
        precisionNumber = int(precision)
        if precisionNumber == 0:
            return '1'
        parsedPrecision = '0.'
        for i in range(0, precisionNumber - 1):
            parsedPrecision = parsedPrecision + '0'
        return parsedPrecision + '1'

    async def load_time_difference(self, params={}):
        serverTime = await self.fetch_time(params)
        after = self.milliseconds()
        self.options['timeDifference'] = after - serverTime
        return self.options['timeDifference']

    def implode_hostname(self, url: str):
        return self.implode_params(url, {'hostname': self.hostname})

    async def fetch_market_leverage_tiers(self, symbol: str, params={}):
        if self.has['fetchLeverageTiers']:
            market = self.market(symbol)
            if not market['contract']:
                raise BadSymbol(self.id + ' fetchMarketLeverageTiers() supports contract markets only')
            tiers = await self.fetch_leverage_tiers([symbol])
            return self.safe_value(tiers, symbol)
        else:
            raise NotSupported(self.id + ' fetchMarketLeverageTiers() is not supported yet')

    async def create_post_only_order(self, symbol: str, type: OrderType, side: OrderSide, amount, price, params={}):
        if not self.has['createPostOnlyOrder']:
            raise NotSupported(self.id + 'createPostOnlyOrder() is not supported yet')
        query = self.extend(params, {'postOnly': True})
        return await self.create_order(symbol, type, side, amount, price, query)

    async def create_reduce_only_order(self, symbol: str, type: OrderType, side: OrderSide, amount, price, params={}):
        if not self.has['createReduceOnlyOrder']:
            raise NotSupported(self.id + 'createReduceOnlyOrder() is not supported yet')
        query = self.extend(params, {'reduceOnly': True})
        return await self.create_order(symbol, type, side, amount, price, query)

    async def create_stop_order(self, symbol: str, type: OrderType, side: OrderSide, amount, price=None, stopPrice=None, params={}):
        if not self.has['createStopOrder']:
            raise NotSupported(self.id + ' createStopOrder() is not supported yet')
        if stopPrice is None:
            raise ArgumentsRequired(self.id + ' create_stop_order() requires a stopPrice argument')
        query = self.extend(params, {'stopPrice': stopPrice})
        return await self.create_order(symbol, type, side, amount, price, query)

    async def create_stop_limit_order(self, symbol: str, side: OrderSide, amount, price, stopPrice, params={}):
        if not self.has['createStopLimitOrder']:
            raise NotSupported(self.id + ' createStopLimitOrder() is not supported yet')
        query = self.extend(params, {'stopPrice': stopPrice})
        return await self.create_order(symbol, 'limit', side, amount, price, query)

    async def create_stop_market_order(self, symbol: str, side: OrderSide, amount, stopPrice, params={}):
        if not self.has['createStopMarketOrder']:
            raise NotSupported(self.id + ' createStopMarketOrder() is not supported yet')
        query = self.extend(params, {'stopPrice': stopPrice})
        return await self.create_order(symbol, 'market', side, amount, None, query)

    def safe_currency_code(self, currencyId: Optional[str], currency: Optional[Any] = None):
        currency = self.safe_currency(currencyId, currency)
        return currency['code']

    def filter_by_symbol_since_limit(self, array, symbol: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, tail=False):
        return self.filter_by_value_since_limit(array, 'symbol', symbol, since, limit, 'timestamp', tail)

    def filter_by_currency_since_limit(self, array, code=None, since: Optional[int] = None, limit: Optional[int] = None, tail=False):
        return self.filter_by_value_since_limit(array, 'currency', code, since, limit, 'timestamp', tail)

    def parse_last_prices(self, pricesData, symbols: Optional[List[str]] = None, params={}):
        #
        # the value of tickers is either a dict or a list
        #
        # dict
        #
        #     {
        #         'marketId1': {...},
        #         'marketId2': {...},
        #         ...
        #     }
        #
        # list
        #
        #     [
        #         {'market': 'marketId1', ...},
        #         {'market': 'marketId2', ...},
        #         ...
        #     ]
        #
        results = []
        if isinstance(pricesData, list):
            for i in range(0, len(pricesData)):
                priceData = self.extend(self.parseLastPrice(pricesData[i]), params)
                results.append(priceData)
        else:
            marketIds = list(pricesData.keys())
            for i in range(0, len(marketIds)):
                marketId = marketIds[i]
                market = self.safe_market(marketId)
                priceData = self.extend(self.parseLastPrice(pricesData[marketId], market), params)
                results.append(priceData)
        symbols = self.market_symbols(symbols)
        return self.filter_by_array(results, 'symbol', symbols)

    def parse_tickers(self, tickers, symbols: Optional[List[str]] = None, params={}):
        #
        # the value of tickers is either a dict or a list
        #
        # dict
        #
        #     {
        #         'marketId1': {...},
        #         'marketId2': {...},
        #         'marketId3': {...},
        #         ...
        #     }
        #
        # list
        #
        #     [
        #         {'market': 'marketId1', ...},
        #         {'market': 'marketId2', ...},
        #         {'market': 'marketId3', ...},
        #         ...
        #     ]
        #
        results = []
        if isinstance(tickers, list):
            for i in range(0, len(tickers)):
                ticker = self.extend(self.parse_ticker(tickers[i]), params)
                results.append(ticker)
        else:
            marketIds = list(tickers.keys())
            for i in range(0, len(marketIds)):
                marketId = marketIds[i]
                market = self.safe_market(marketId)
                ticker = self.extend(self.parse_ticker(tickers[marketId], market), params)
                results.append(ticker)
        symbols = self.market_symbols(symbols)
        return self.filter_by_array(results, 'symbol', symbols)

    def parse_deposit_addresses(self, addresses, codes: Optional[List[str]] = None, indexed=True, params={}):
        result = []
        for i in range(0, len(addresses)):
            address = self.extend(self.parse_deposit_address(addresses[i]), params)
            result.append(address)
        if codes is not None:
            result = self.filter_by_array(result, 'currency', codes, False)
        if indexed:
            return self.index_by(result, 'currency')
        return result

    def parse_borrow_interests(self, response, market=None):
        interests = []
        for i in range(0, len(response)):
            row = response[i]
            interests.append(self.parse_borrow_interest(row, market))
        return interests

    def parse_funding_rate_histories(self, response, market=None, since: Optional[int] = None, limit: Optional[int] = None):
        rates = []
        for i in range(0, len(response)):
            entry = response[i]
            rates.append(self.parse_funding_rate_history(entry, market))
        sorted = self.sort_by(rates, 'timestamp')
        symbol = None if (market is None) else market['symbol']
        return self.filter_by_symbol_since_limit(sorted, symbol, since, limit)

    def safe_symbol(self, marketId, market=None, delimiter=None, marketType=None):
        market = self.safe_market(marketId, market, delimiter, marketType)
        return market['symbol']

    def parse_funding_rate(self, contract: str, market=None):
        raise NotSupported(self.id + ' parseFundingRate() is not supported yet')

    def parse_funding_rates(self, response, market=None):
        result = {}
        for i in range(0, len(response)):
            parsed = self.parse_funding_rate(response[i], market)
            result[parsed['symbol']] = parsed
        return result

    def is_trigger_order(self, params):
        isTrigger = self.safe_value_2(params, 'trigger', 'stop')
        if isTrigger:
            params = self.omit(params, ['trigger', 'stop'])
        return [isTrigger, params]

    def is_post_only(self, isMarketOrder: bool, exchangeSpecificParam, params={}):
        """
         * @ignore
        :param str type: Order type
        :param boolean exchangeSpecificParam: exchange specific postOnly
        :param dict [params]: exchange specific params
        :returns boolean: True if a post only order, False otherwise
        """
        timeInForce = self.safe_string_upper(params, 'timeInForce')
        postOnly = self.safe_value_2(params, 'postOnly', 'post_only', False)
        # we assume timeInForce is uppercase from safeStringUpper(params, 'timeInForce')
        ioc = timeInForce == 'IOC'
        fok = timeInForce == 'FOK'
        timeInForcePostOnly = timeInForce == 'PO'
        postOnly = postOnly or timeInForcePostOnly or exchangeSpecificParam
        if postOnly:
            if ioc or fok:
                raise InvalidOrder(self.id + ' postOnly orders cannot have timeInForce equal to ' + timeInForce)
            elif isMarketOrder:
                raise InvalidOrder(self.id + ' market orders cannot be postOnly')
            else:
                return True
        else:
            return False

    def handle_post_only(self, isMarketOrder: bool, exchangeSpecificPostOnlyOption: bool, params: Any = {}):
        """
         * @ignore
        :param str type: Order type
        :param boolean exchangeSpecificBoolean: exchange specific postOnly
        :param dict [params]: exchange specific params
        :returns Array:
        """
        timeInForce = self.safe_string_upper(params, 'timeInForce')
        postOnly = self.safe_value(params, 'postOnly', False)
        ioc = timeInForce == 'IOC'
        fok = timeInForce == 'FOK'
        po = timeInForce == 'PO'
        postOnly = postOnly or po or exchangeSpecificPostOnlyOption
        if postOnly:
            if ioc or fok:
                raise InvalidOrder(self.id + ' postOnly orders cannot have timeInForce equal to ' + timeInForce)
            elif isMarketOrder:
                raise InvalidOrder(self.id + ' market orders cannot be postOnly')
            else:
                if po:
                    params = self.omit(params, 'timeInForce')
                params = self.omit(params, 'postOnly')
                return [True, params]
        return [False, params]

    async def fetch_last_prices(self, symbols: Optional[List[str]] = None, params={}):
        raise NotSupported(self.id + ' fetchLastPrices() is not supported yet')

    async def fetch_trading_fees(self, params={}):
        raise NotSupported(self.id + ' fetchTradingFees() is not supported yet')

    async def fetch_trading_fee(self, symbol: str, params={}):
        if not self.has['fetchTradingFees']:
            raise NotSupported(self.id + ' fetchTradingFee() is not supported yet')
        return await self.fetch_trading_fees(params)

    def parse_open_interest(self, interest, market=None):
        raise NotSupported(self.id + ' parseOpenInterest() is not supported yet')

    def parse_open_interests(self, response, market=None, since: Optional[int] = None, limit: Optional[int] = None):
        interests = []
        for i in range(0, len(response)):
            entry = response[i]
            interest = self.parse_open_interest(entry, market)
            interests.append(interest)
        sorted = self.sort_by(interests, 'timestamp')
        symbol = self.safe_string(market, 'symbol')
        return self.filter_by_symbol_since_limit(sorted, symbol, since, limit)

    async def fetch_funding_rate(self, symbol: str, params={}):
        if self.has['fetchFundingRates']:
            await self.load_markets()
            market = self.market(symbol)
            symbol = market['symbol']
            if not market['contract']:
                raise BadSymbol(self.id + ' fetchFundingRate() supports contract markets only')
            rates = await self.fetchFundingRates([symbol], params)
            rate = self.safe_value(rates, symbol)
            if rate is None:
                raise NullResponse(self.id + ' fetchFundingRate() returned no data for ' + symbol)
            else:
                return rate
        else:
            raise NotSupported(self.id + ' fetchFundingRate() is not supported yet')

    async def fetch_mark_ohlcv(self, symbol, timeframe='1m', since: Optional[int] = None, limit: Optional[int] = None, params={}):
        """
        fetches historical mark price candlestick data containing the open, high, low, and close price of a market
        :param str symbol: unified symbol of the market to fetch OHLCV data for
        :param str timeframe: the length of time each candle represents
        :param int [since]: timestamp in ms of the earliest candle to fetch
        :param int [limit]: the maximum amount of candles to fetch
        :param dict [params]: extra parameters specific to the exchange api endpoint
        :returns float[][]: A list of candles ordered, open, high, low, close, None
        """
        if self.has['fetchMarkOHLCV']:
            request = {
                'price': 'mark',
            }
            return await self.fetch_ohlcv(symbol, timeframe, since, limit, self.extend(request, params))
        else:
            raise NotSupported(self.id + ' fetchMarkOHLCV() is not supported yet')

    async def fetch_index_ohlcv(self, symbol: str, timeframe='1m', since: Optional[int] = None, limit: Optional[int] = None, params={}):
        """
        fetches historical index price candlestick data containing the open, high, low, and close price of a market
        :param str symbol: unified symbol of the market to fetch OHLCV data for
        :param str timeframe: the length of time each candle represents
        :param int [since]: timestamp in ms of the earliest candle to fetch
        :param int [limit]: the maximum amount of candles to fetch
        :param dict [params]: extra parameters specific to the exchange api endpoint
         * @returns {} A list of candles ordered, open, high, low, close, None
        """
        if self.has['fetchIndexOHLCV']:
            request = {
                'price': 'index',
            }
            return await self.fetch_ohlcv(symbol, timeframe, since, limit, self.extend(request, params))
        else:
            raise NotSupported(self.id + ' fetchIndexOHLCV() is not supported yet')

    async def fetch_premium_index_ohlcv(self, symbol: str, timeframe='1m', since: Optional[int] = None, limit: Optional[int] = None, params={}):
        """
        fetches historical premium index price candlestick data containing the open, high, low, and close price of a market
        :param str symbol: unified symbol of the market to fetch OHLCV data for
        :param str timeframe: the length of time each candle represents
        :param int [since]: timestamp in ms of the earliest candle to fetch
        :param int [limit]: the maximum amount of candles to fetch
        :param dict [params]: extra parameters specific to the exchange api endpoint
        :returns float[][]: A list of candles ordered, open, high, low, close, None
        """
        if self.has['fetchPremiumIndexOHLCV']:
            request = {
                'price': 'premiumIndex',
            }
            return await self.fetch_ohlcv(symbol, timeframe, since, limit, self.extend(request, params))
        else:
            raise NotSupported(self.id + ' fetchPremiumIndexOHLCV() is not supported yet')

    def handle_time_in_force(self, params={}):
        """
         * @ignore
         * * Must add timeInForce to self.options to use self method
        :return string returns: the exchange specific value for timeInForce
        """
        timeInForce = self.safe_string_upper(params, 'timeInForce')  # supported values GTC, IOC, PO
        if timeInForce is not None:
            exchangeValue = self.safe_string(self.options['timeInForce'], timeInForce)
            if exchangeValue is None:
                raise ExchangeError(self.id + ' does not support timeInForce "' + timeInForce + '"')
            return exchangeValue
        return None

    def convert_type_to_account(self, account):
        """
         * @ignore
         * * Must add accountsByType to self.options to use self method
        :param str account: key for account name in self.options['accountsByType']
        :returns: the exchange specific account name or the isolated margin id for transfers
        """
        accountsByType = self.safe_value(self.options, 'accountsByType', {})
        lowercaseAccount = account.lower()
        if lowercaseAccount in accountsByType:
            return accountsByType[lowercaseAccount]
        elif (account in self.markets) or (account in self.markets_by_id):
            market = self.market(account)
            return market['id']
        else:
            return account

    def check_required_argument(self, methodName, argument, argumentName, options=[]):
        """
         * @ignore
        :param str methodName: the name of the method that the argument is being checked for
        :param str argument: the argument's actual value provided
        :param str argumentName: the name of the argument being checked(for logging purposes)
        :param str[] options: a list of options that the argument can be
        :returns None:
        """
        optionsLength = len(options)
        if (argument is None) or ((optionsLength > 0) and (not(self.in_array(argument, options)))):
            messageOptions = ', '.join(options)
            message = self.id + ' ' + methodName + '() requires a ' + argumentName + ' argument'
            if messageOptions != '':
                message += ', one of ' + '(' + messageOptions + ')'
            raise ArgumentsRequired(message)

    def check_required_margin_argument(self, methodName: str, symbol: str, marginMode: str):
        """
         * @ignore
        :param str symbol: unified symbol of the market
        :param str methodName: name of the method that requires a symbol
        :param str marginMode: is either 'isolated' or 'cross'
        """
        if (marginMode == 'isolated') and (symbol is None):
            raise ArgumentsRequired(self.id + ' ' + methodName + '() requires a symbol argument for isolated margin')
        elif (marginMode == 'cross') and (symbol is not None):
            raise ArgumentsRequired(self.id + ' ' + methodName + '() cannot have a symbol argument for cross margin')

    def check_required_symbol(self, methodName: str, symbol: str):
        """
         * @ignore
        :param str symbol: unified symbol of the market
        :param str methodName: name of the method that requires a symbol
        """
        self.check_required_argument(methodName, symbol, 'symbol')

    def parse_deposit_withdraw_fees(self, response, codes: Optional[List[str]] = None, currencyIdKey=None):
        """
         * @ignore
        :param object[]|dict response: unparsed response from the exchange
        :param str[]|None codes: the unified currency codes to fetch transactions fees for, returns all currencies when None
        :param str currencyIdKey: *should only be None when response is a dictionary* the object key that corresponds to the currency id
        :returns dict: objects with withdraw and deposit fees, indexed by currency codes
        """
        depositWithdrawFees = {}
        codes = self.marketCodes(codes)
        isArray = isinstance(response, list)
        responseKeys = response
        if not isArray:
            responseKeys = list(response.keys())
        for i in range(0, len(responseKeys)):
            entry = responseKeys[i]
            dictionary = entry if isArray else response[entry]
            currencyId = self.safe_string(dictionary, currencyIdKey) if isArray else entry
            currency = self.safe_value(self.currencies_by_id, currencyId)
            code = self.safe_string(currency, 'code', currencyId)
            if (codes is None) or (self.in_array(code, codes)):
                depositWithdrawFees[code] = self.parseDepositWithdrawFee(dictionary, currency)
        return depositWithdrawFees

    def parse_deposit_withdraw_fee(self, fee, currency=None):
        raise NotSupported(self.id + ' parseDepositWithdrawFee() is not supported yet')

    def deposit_withdraw_fee(self, info):
        return {
            'info': info,
            'withdraw': {
                'fee': None,
                'percentage': None,
            },
            'deposit': {
                'fee': None,
                'percentage': None,
            },
            'networks': {},
        }

    def assign_default_deposit_withdraw_fees(self, fee, currency=None):
        """
         * @ignore
        Takes a depositWithdrawFee structure and assigns the default values for withdraw and deposit
        :param dict fee: A deposit withdraw fee structure
        :param dict currency: A currency structure, the response from self.currency()
        :returns dict: A deposit withdraw fee structure
        """
        networkKeys = list(fee['networks'].keys())
        numNetworks = len(networkKeys)
        if numNetworks == 1:
            fee['withdraw'] = fee['networks'][networkKeys[0]]['withdraw']
            fee['deposit'] = fee['networks'][networkKeys[0]]['deposit']
            return fee
        currencyCode = self.safe_string(currency, 'code')
        for i in range(0, numNetworks):
            network = networkKeys[i]
            if network == currencyCode:
                fee['withdraw'] = fee['networks'][networkKeys[i]]['withdraw']
                fee['deposit'] = fee['networks'][networkKeys[i]]['deposit']
        return fee

    def parse_income(self, info, market=None):
        raise NotSupported(self.id + ' parseIncome() is not supported yet')

    def parse_incomes(self, incomes, market=None, since: Optional[int] = None, limit: Optional[int] = None):
        """
         * @ignore
        parses funding fee info from exchange response
        :param dict[] incomes: each item describes once instance of currency being received or paid
        :param dict market: ccxt market
        :param int [since]: when defined, the response items are filtered to only include items after self timestamp
        :param int [limit]: limits the number of items in the response
        :returns dict[]: an array of `funding history structures <https://github.com/ccxt/ccxt/wiki/Manual#funding-history-structure>`
        """
        result = []
        for i in range(0, len(incomes)):
            entry = incomes[i]
            parsed = self.parse_income(entry, market)
            result.append(parsed)
        sorted = self.sort_by(result, 'timestamp')
        return self.filter_by_since_limit(sorted, since, limit)

    def get_market_from_symbols(self, symbols: Optional[List[str]] = None):
        if symbols is None:
            return None
        firstMarket = self.safe_string(symbols, 0)
        market = self.market(firstMarket)
        return market

    def parse_ws_ohlcvs(self, ohlcvs: List[object], market: Optional[Any] = None, timeframe: str = '1m', since: Optional[int] = None, limit: Optional[int] = None):
        results = []
        for i in range(0, len(ohlcvs)):
            results.append(self.parse_ws_ohlcv(ohlcvs[i], market))
        return results

    async def fetch_transactions(self, code: Optional[str] = None, since: Optional[int] = None, limit: Optional[int] = None, params={}):
        """
         * @deprecated
        *DEPRECATED* use fetchDepositsWithdrawals instead
        :param str code: unified currency code for the currency of the deposit/withdrawals, default is None
        :param int [since]: timestamp in ms of the earliest deposit/withdrawal, default is None
        :param int [limit]: max number of deposit/withdrawals to return, default is None
        :param dict [params]: extra parameters specific to the exchange api endpoint
        :returns dict: a list of `transaction structures <https://github.com/ccxt/ccxt/wiki/Manual#transaction-structure>`
        """
        if self.has['fetchDepositsWithdrawals']:
            return await self.fetchDepositsWithdrawals(code, since, limit, params)
        else:
            raise NotSupported(self.id + ' fetchTransactions() is not supported yet')

    def filter_by_array_positions(self, objects, key: IndexType, values=None, indexed=True):
        """
         * @ignore
        Typed wrapper for filterByArray that returns a list of positions
        """
        return self.filter_by_array(objects, key, values, indexed)

    def resolve_promise_if_messagehash_matches(self, client, prefix: str, symbol: str, data):
        messageHashes = self.findMessageHashes(client, prefix)
        for i in range(0, len(messageHashes)):
            messageHash = messageHashes[i]
            parts = messageHash.split('::')
            symbolsString = parts[1]
            symbols = symbolsString.split(',')
            if self.in_array(symbol, symbols):
                client.resolve(data, messageHash)

    def resolve_multiple_ohlcv(self, client, prefix: str, symbol: str, timeframe: str, data):
        messageHashes = self.findMessageHashes(client, 'multipleOHLCV::')
        for i in range(0, len(messageHashes)):
            messageHash = messageHashes[i]
            parts = messageHash.split('::')
            symbolsAndTimeframes = parts[1]
            splitted = symbolsAndTimeframes.split(',')
            id = symbol + '#' + timeframe
            if self.in_array(id, splitted):
                client.resolve([symbol, timeframe, data], messageHash)

    def create_ohlcv_object(self, symbol: str, timeframe: str, data):
        res = {}
        res[symbol] = {}
        res[symbol][timeframe] = data
        return res
