import os
import sys
import time
import json
import random
import logging
import asyncio
import argparse
from quart import Quart, request, jsonify
from camoufox.async_api import AsyncCamoufox
from patchright.async_api import async_playwright


COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("TurnstileAPIServer")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Turnstile Solver</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
        <script>
            async function fetchIP() {
                try {
                    const response = await fetch('https://api64.ipify.org?format=json');
                    const data = await response.json();
                    document.getElementById('ip-display').innerText = `Your IP: ${data.ip}`;
                } catch (error) {
                    console.error('Error fetching IP:', error);
                    document.getElementById('ip-display').innerText = 'Failed to fetch IP';
                }
            }
            window.onload = fetchIP;
        </script>
    </head>
    <body>
        <!-- cf turnstile -->
        <p id="ip-display">Fetching your IP...</p>
    </body>
    </html>
    """

    def __init__(self, headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.useragent = useragent
        self.thread_count = max(1, min(thread, 10))
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.browser_args = []
        self.playwright_instance = None
        self.camoufox_instance = None
        self.initialized = False
        
        if useragent:
            self.browser_args.append(f"--user-agent={useragent}")

        self._setup_routes()

    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/')(self.index)
        self.app.route('/health', methods=['GET'])(self.health_check)

    async def health_check(self):
        """Health check endpoint for Railway."""
        return jsonify({
            "status": "healthy",
            "browser_pool_size": self.browser_pool.qsize(),
            "browser_type": self.browser_type,
            "headless": self.headless,
            "thread_count": self.thread_count,
            "initialized": self.initialized
        })

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Starting browser initialization")
        try:
            await self._initialize_browser()
            self.initialized = True
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        playwright = None
        
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            self.playwright_instance = await async_playwright().start()
            playwright = self.playwright_instance
        elif self.browser_type == "camoufox":
            self.camoufox_instance = AsyncCamoufox(headless=self.headless)

        for i in range(self.thread_count):
            try:
                if self.browser_type in ['chromium', 'chrome', 'msedge']:
                    browser = await playwright.chromium.launch(
                        channel=self.browser_type,
                        headless=self.headless,
                        args=self.browser_args + [
                            '--no-sandbox',
                            '--disable-setuid-sandbox',
                            '--disable-dev-shm-usage',
                            '--disable-accelerated-2d-canvas',
                            '--disable-gpu'
                        ]
                    )
                elif self.browser_type == "camoufox":
                    browser = await self.camoufox_instance.start()

                await self.browser_pool.put((i + 1, browser))
                if self.debug:
                    logger.success(f"Browser {i + 1} initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize browser {i + 1}: {str(e)}")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

    async def _solve_turnstile(self, url: str, sitekey: str, action: str = None, cdata: str = None):
        """Solve the Turnstile challenge and capture cookies."""
        proxy = None
        index, browser = await self.browser_pool.get()
        context = None
        start_time = time.time()

        try:
            if self.proxy_support:
                proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
                if os.path.exists(proxy_file_path):
                    with open(proxy_file_path) as proxy_file:
                        proxies = [line.strip() for line in proxy_file if line.strip()]
                    proxy = random.choice(proxies) if proxies else None

                    if proxy:
                        parts = proxy.split(':')
                        if len(parts) == 3:
                            context = await browser.new_context(proxy={"server": proxy})
                        elif len(parts) == 5:
                            proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
                            context = await browser.new_context(proxy={
                                "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                                "username": proxy_user,
                                "password": proxy_pass
                            })
                        else:
                            context = await browser.new_context()
                    else:
                        context = await browser.new_context()
                else:
                    context = await browser.new_context()
            else:
                context = await browser.new_context()

            await context.clear_cookies()
            if self.debug:
                logger.debug(f"Browser {index}: Cleared all existing cookies before starting")

            page = await context.new_page()

            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url}")

            url_with_slash = url + "/" if not url.endswith("/") else url
            turnstile_div = f'<div class="cf-turnstile" style="background: white;" data-sitekey="{sitekey}"' \
                            + (f' data-action="{action}"' if action else '') \
                            + (f' data-cdata="{cdata}"' if cdata else '') + '></div>'
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)

            await page.route(url_with_slash, lambda route: route.fulfill(body=page_data, status=200))
            await page.goto(url_with_slash)

            await page.eval_on_selector("//div[@class='cf-turnstile']", "el => el.style.width = '70px'")
            
            result = {
                "value": "CAPTCHA_FAIL",
                "elapsed_time": 0,
                "cookies": []
            }

            for attempt in range(20):
                try:
                    turnstile_check = await page.input_value("[name=cf-turnstile-response]", timeout=2000)
                    if turnstile_check == "":
                        if self.debug:
                            logger.debug(f"Browser {index}: Attempt {attempt} - No Turnstile response yet")
                        try:
                            await page.locator("//div[@class='cf-turnstile']").click(timeout=1000)
                        except:
                            pass
                        await asyncio.sleep(0.5)
                    else:
                        elapsed_time = round(time.time() - start_time, 3)

                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                        except:
                            pass
                        
                        cookies = await context.cookies()

                        logger.success(
                            f"Browser {index}: Successfully solved captcha - "
                            f"{COLORS.get('MAGENTA')}{turnstile_check[:10]}{COLORS.get('RESET')} "
                            f"in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds"
                        )

                        result = {
                            "value": turnstile_check,
                            "elapsed_time": elapsed_time,
                            "cookies": cookies
                        }
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Attempt {attempt} - {str(e)}")

            if result["value"] == "CAPTCHA_FAIL":
                elapsed_time = round(time.time() - start_time, 3)
                result["elapsed_time"] = elapsed_time
                if self.debug:
                    logger.error(f"Browser {index}: Error solving Turnstile in {elapsed_time} Seconds")

            return result

        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
            return {
                "value": "CAPTCHA_FAIL",
                "elapsed_time": elapsed_time,
                "cookies": []
            }

        finally:
            try:
                if context:
                    await context.clear_cookies()
                    if self.debug:
                        logger.debug(f"Browser {index}: Cleared cookies before closing context")
                    await context.close()
            except Exception as e:
                logger.error(f"Browser {index}: Error while closing context: {str(e)}")
            finally:
                await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "error": "Both 'url' and 'sitekey' are required"
            }), 400

        try:
            result = await self._solve_turnstile(url=url, sitekey=sitekey, action=action, cdata=cdata)
            
            if result["value"] == "CAPTCHA_FAIL":
                response = jsonify({
                    "status": "error",
                    "error": "Failed to solve Turnstile",
                    "elapsed_time": result["elapsed_time"]
                })
                response.status_code = 422
                return response
            
            response = jsonify({
                "status": "success",
                "token": result["value"],
                "elapsed_time": result["elapsed_time"],
                "cookies": {cookie['name']: cookie['value'] for cookie in result.get('cookies', [])}
            })
            
            response.headers['X-Turnstile-Token'] = result["value"]
            
            for cookie in result.get('cookies', []):
                if cookie['name'] in ['cf_clearance', '__cf_bm']:
                    response.set_cookie(
                        key=cookie['name'],
                        value=cookie['value'],
                        domain=cookie.get('domain') or '.cloudflare.com',
                        path=cookie.get('path', '/'),
                        expires=cookie.get('expires'),
                        secure=True,
                        httponly=True,
                        samesite='None'
                    )
                else:
                    response.set_cookie(
                        key=cookie['name'],
                        value=cookie['value'],
                        domain=cookie.get('domain'),
                        path=cookie.get('path', '/'),
                        expires=cookie.get('expires'),
                        secure=cookie.get('secure', False),
                        httponly=cookie.get('httpOnly', False),
                        samesite=cookie.get('sameSite', 'Lax')
                    )
            
            return response

        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            response = jsonify({
                "status": "error",
                "error": str(e)
            })
            response.status_code = 500
            return response

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">Send a GET request to 
                       <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with the following query parameters:</p>

                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: The URL where Turnstile is to be validated</li>
                        <li><strong>sitekey</strong>: The site key for Turnstile</li>
                        <li><strong>action</strong>: (Optional) Action parameter</li>
                        <li><strong>cdata</strong>: (Optional) CData parameter</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=0x4AAAAAAA...</code>
                    </div>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Health Check:</p>
                        <code class="text-sm break-all text-red-300">/health</code>
                    </div>

                    <div class="bg-red-900 border-l-4 border-red-600 p-4">
                        <p class="text-red-200 font-semibold">Maintained by 
                           <a href="https://github.com/Theyka" class="text-red-300 hover:underline">Theyka</a> 
                           and <a href="https://github.com/sexfrance" class="text-red-300 hover:underline">Sexfrance</a></p>
                    </div>
                </div>
            </body>
            </html>
        """


# Create the app instance
app = Quart(__name__)
server = None


@app.before_serving
async def startup():
    global server
    
    headless = os.getenv('HEADLESS', 'true').lower() == 'true'
    browser_type = os.getenv('BROWSER_TYPE', 'chromium')
    thread_count = int(os.getenv('THREAD_COUNT', '3'))
    debug = os.getenv('DEBUG', 'false').lower() == 'true'
    useragent = os.getenv('USER_AGENT', None)
    proxy_support = os.getenv('PROXY_SUPPORT', 'false').lower() == 'true'
    
    browser_types = ['chromium', 'chrome', 'msedge', 'camoufox']
    if browser_type not in browser_types:
        logger.error(f"Unknown browser type: {browser_type}")
        raise ValueError(f"Unknown browser type: {browser_type}")
    
    server = TurnstileAPIServer(
        headless=headless,
        useragent=useragent,
        debug=debug,
        browser_type=browser_type,
        thread=thread_count,
        proxy_support=proxy_support
    )
    
    # Copy routes from server to app
    app.route('/turnstile', methods=['GET'])(server.process_turnstile)
    app.route('/')(server.index)
    app.route('/health', methods=['GET'])(server.health_check)
    
    await server._startup()


if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)