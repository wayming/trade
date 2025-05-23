import time
from collections import OrderedDict
import re
from typing import List

import requests
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from .lru_cache import LRUCache
from .interface import NewsScraper
import traceback
import pika

QUEUE_IV_ARTICLES = "iv_articles"

class InvestingScraper(NewsScraper):
    def __init__(
            self,
            queue_conn: pika.BlockingConnection = None,
            queue_name = QUEUE_IV_ARTICLES):
        self.driver = None
        self.article_cache = LRUCache(20)
        self.queue_name = queue_name
        
        if queue_conn != None:
            self.queue_channel = queue_conn.channel()
            self.queue_channel.queue_declare(queue=queue_name)

    def _start_driver(self):
        options = uc.ChromeOptions()
        options.add_argument("--disable-blink-features=AutomationControlled")

        options.add_argument("--disable-infobars")
        options.add_argument("--disable-extensions")
        options.add_argument("--start-maximized")

        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-software-rasterizer")  # Disable software rasterization
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36")
        
        # options.add_argument("--headless=new")

        self.driver = uc.Chrome(options=options, use_subprocess=True)

        # Avoid loading js which blocks the requests
        self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*.js"]})
        self.driver.execute_cdp_cmd("Network.enable", {})
    
        # Enable logging of browser console messages
        self.driver.execute_cdp_cmd('Log.enable', {})

        return self.driver
     
    def _slugify(self, text, max_length=100):
        text = re.sub(r'[<>:"/\\|?*\s,\.]', '_', text.strip("'"))
        return text[:max_length]

    def login(self) -> bool:
        self.driver = self._start_driver()
        return True

    def fetch_news(self, limit=5) -> List[str]:
        print("\n" + "="*50)
        print(f"Starting new scan(au.investing.com) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*50)

        file_paths = []

        try:
            url = 'https://au.investing.com/news/headlines'
            self.driver.get(url)

            print("Waiting page to load")
            # wait page to load
            WebDriverWait(self.driver, 60).until(
                EC.text_to_be_present_in_element(
                    (By.CSS_SELECTOR, "h1.text-xl\\/7.sm\\:text-3xl\\/8.font-bold"), 
                    "Breaking News"
                )
            )
            print("Page loaded")

            # Extract news (adjust selectors)
            news_items = self.driver.find_elements(By.CSS_SELECTOR, '.inline-block')
            links = [el.get_attribute("href") for el in news_items if el.get_attribute("href")]
            titles = [el.text.strip() for el in news_items]
            new_articles_found = 0
            for link, title in zip(links[:5], titles[:5]):  #(first 5)
                # Skip if article is already in cache
                if self.article_cache.get(link):
                    print(f"\nSkipping cached article: {link}")
                    continue

                print(f"\nReading new article.")
                print(f"title: {title}\nlink: {link}\n")

                try:
                    # news contents
                    self.driver.get(link)
                    WebDriverWait(self.driver, 15).until(
                        EC.presence_of_element_located((By.ID, "articleTitle"))
                    )

                    # Save page HTML to file
                    fname = self._slugify(title)
                    html_path = f"output/{fname}.html"
                    html_content = self.driver.page_source
                    
                    # Save to file 
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(html_content)
                    print(f"Saved HTML to {html_path}")
                    file_paths.append(html_path)

                    # Send HTML to RabbitMQ
                    if self.queue_channel != None:
                        self.queue_channel.basic_publish(
                            exchange='',
                            routing_key=self.queue_name,
                            body=html_content.encode('utf-8')
                        )
                    print(f"Sent article to queue: {fname}")

                    # Add to cache
                    self.article_cache.put(link)
                    file_paths.append(html_path)
                    new_articles_found += 1
                except Exception as e:
                    print(f"Failed to read article: {e}")

            if new_articles_found == 0:
                print("\nNo new articles found in this scan")
            
        except Exception as e:
            self.driver.save_screenshot(f"output/investing_error.png")
            print(f"An error occurred when reading new messages: {e}")


        return file_paths

def rabbit_mq_connect() -> pika.BlockingConnection:
    while True:
        try:
            print("[Scraper_Investing] Connecting to RabbitMQ...")
            rabbit_connection = pika.BlockingConnection(
                pika.ConnectionParameters(host="rabbitmq")
            )
            print("[Scraper_Investing] Connected to RabbitMQ.")
            return rabbit_connection
        except pika.exceptions.AMQPConnectionError:
            print("[Scraper_Investing] Waiting for RabbitMQ...")
            time.sleep(2)


def main():
    mq_conn = rabbit_mq_connect()
    scraper = InvestingScraper(mq_conn, QUEUE_IV_ARTICLES)
    if not scraper.login():
        return

    while True:
        articles = scraper.fetch_news(limit=5)
        print(articles)
        time.sleep(10)

if __name__ == "__main__":
    main()