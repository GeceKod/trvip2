import re
import sys
import time
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

# --- YENİ ---
# Güncel adresi bulmak için kullanılacak portal adresi
PORTAL_DOMAIN = "https://www.selcuksportshd.is/"

# --- YENİ ---
# com.gunes.tv uygulamanızın global olarak kullanmasını istediğiniz User-Agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

def find_working_domain(page):
    """
    Portal sayfasını ziyaret eder ve 'a.site-button' class'ına sahip
    elementin href özelliğinden güncel domain'i çeker.
    """
    print(f"\n🔎 Güncel domain {PORTAL_DOMAIN} adresinden alınıyor...")
    try:
        page.goto(PORTAL_DOMAIN, timeout=20000, wait_until='domcontentloaded')
        
        selector = 'a.site-button:has(img[alt="Site Giriş"])' # Selçuk Sports için
        
        page.wait_for_selector(selector, timeout=10000)
        link_element = page.query_selector(selector)
        
        if not link_element:
             print("-> ❌ Portal sayfasında 'Site Giriş' linki bulunamadı.")
             return None
        
        domain = link_element.get_attribute('href')
        
        if not domain:
            print("-> ❌ Link elementinde 'href' özelliği bulunamadı.")
            return None

        domain = domain.rstrip('/')
        print(f"✅ Güncel domain başarıyla bulundu: {domain}")
        return domain
        
    except Exception as e:
        print(f"❌ Portal sayfasına ulaşılamadı veya domain alınamadı: {e.__class__.__name__}")
        return None

def get_channel_group(channel_name):
    """
    Verilen kanal ismine göre bir grup adı döndürür.
    """
    channel_name_lower = channel_name.lower()
    group_mappings = {
        'BeinSports': ['bein sports', 'beın sports'],
        'S Sports': ['s sport'],
        'Tivibu': ['tivibu spor'],
        'Ulusal Kanallar': ['a spor', 'trt spor', 'trt 1'],
        'Diğer Spor': ['smart spor', 'nba tv', 'eurosport'],
        'Belgesel': ['national geographic', 'nat geo', 'discovery', 'dmax', 'bbc earth', 'history'],
        'Film & Dizi': ['bein series', 'bein movies', 'movie smart']
    }
    for group, keywords in group_mappings.items():
        for keyword in keywords:
            if keyword in channel_name_lower:
                return group
    return "Maç Yayınları"

def scrape_channel_links(page, domain_to_scrape):
    """
    Selçuk Sports ana sayfasını ziyaret eder ve tüm kanalları
    isim, URL, grup ve GEREKLİ REFERER BİLGİSİ (origin) ile toplar.
    """
    print(f"\n📡 Kanallar {domain_to_scrape} adresinden çekiliyor...")
    channels = []
    try:
        page.goto(domain_to_scrape, timeout=25000, wait_until='domcontentloaded')
        
        link_elements = page.query_selector_all("a[data-url]")
        
        if not link_elements:
            print("❌ Ana sayfada 'data-url' içeren hiçbir kanal linki bulunamadı.")
            return []
            
        for link in link_elements:
            player_url = link.get_attribute('data-url')
            name_element = link.query_selector('div.name')
            
            if name_element and player_url:
                channel_name = name_element.inner_text().strip()
                
                if player_url.startswith('/'):
                    base_domain = domain_to_scrape.rstrip('/')
                    player_url = f"{base_domain}{player_url}"
                
                try:
                    parsed_player_url = urlparse(player_url)
                    player_origin = f"{parsed_player_url.scheme}://{parsed_player_url.netloc}"
                except Exception:
                    player_origin = None 
                
                if not player_origin:
                    continue 

                group_name = get_channel_group(channel_name)
                
                channels.append({
                    'name': channel_name,
                    'url': player_url,      
                    'group': group_name,
                    'origin': player_origin 
                })

        print(f"✅ {len(channels)} adet potansiyel kanal linki bulundu ve gruplandırıldı.")
        return channels
        
    except PlaywrightError as e:
        print(f"❌ Selçuk Sports ana sayfasına ulaşılamadı. Hata: {e.__class__.__name__}")
        return []

def extract_m3u8_from_page(page, player_url):
    """
    Oynatıcı sayfasından M3U8 linkini ağ isteklerini (network requests) dinleyerek 
    veya sayfa kaynağından asıl değişkeni bularak oluşturur.
    """
    m3u8_url = None

    # Ağ isteklerini dinleyen fonksiyon
    def handle_request(request):
        nonlocal m3u8_url
        if ".m3u8" in request.url and not m3u8_url:
            m3u8_url = request.url

    try:
        # İsteği dinlemeye başla
        page.on("request", handle_request)
        page.goto(player_url, timeout=20000, wait_until="domcontentloaded")
        
        # Oynatıcının m3u8 isteğini yapması için kısa bir süre bekle
        try:
            page.wait_for_timeout(1500)
        except:
            pass
            
        page.remove_listener("request", handle_request)

        # Eğer arka planda çağırılan asıl m3u8 bulunduysa doğrudan onu döndür
        if m3u8_url:
            return m3u8_url

        # AĞ İSTEĞİ YAKALANAMAZSA: Sayfa kaynağından gerçek ID'yi bul
        content = page.content()
        
        # 1. İhtimal: Sayfa içinde doğrudan playlist linki varsa
        m3u8_match = re.search(r"(https?://[^\s'\"<>]+playlist\.m3u8[^\s'\"<>]*)", content)
        if m3u8_match:
            return m3u8_match.group(1)

        # 2. İhtimal: baseStreamUrl ve asıl yayın (stream) değişkeni
        base_url_match = re.search(r"this\.baseStreamUrl\s*=\s*['\"](https?://.*?)['\"]", content)
        if not base_url_match:
            print(" -> ❌ 'baseStreamUrl' bulunamadı.", end="")
            return None
            
        base_url = base_url_match.group(1)
        
        # Sayfa içerisindeki muhtemel GERÇEK yayın değişkenlerini ara (örn: streamName="selcukobs1")
        stream_id_match = re.search(r"(?:streamName|streamId|channelId|play_id|id)\s*[:=]\s*['\"]([^'\"]+)['\"]", content)
        
        if stream_id_match:
            stream_id = stream_id_match.group(1)
        else:
            # Tüm çabalar başarısız olursa, en son ihtimal URL'deki id parametresini al
            parsed_url = urlparse(player_url)
            query_params = parse_qs(parsed_url.query)
            stream_id = query_params.get('id', [None])[0]

        if not stream_id:
            print(" -> ❌ Kanal ID'si bulunamadı.", end="")
            return None

        return f"{base_url}{stream_id}/playlist.m3u8"

    except Exception:
        print(" -> ❌ Sayfa yüklenirken hata oluştu.", end="")
        return None
    finally:
        try:
            page.remove_listener("request", handle_request)
        except:
            pass

# --- GÜNCELLENEN MAIN FONKSİYONU ---
def main():
    with sync_playwright() as p:
        print("🚀 Playwright ile Selçuk M3U8 Kanal İndirici Başlatılıyor...")
        
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
        )
        page = context.new_page()

        # Hata Düzeltme: Değişken adı tutarlılığı sağlandı.
        selcuksports_domain = find_working_domain(page)

        if not selcuksports_domain:
            print("❌ UYARI: Güncel domain portal sayfasından alınamadı. İşlem sonlandırılıyor.")
            browser.close()
            sys.exit(1)

        # Hata Düzeltme: selcuksports_domain kullanıldı.
        channels = scrape_channel_links(page, selcuksports_domain)

        if not channels:
            print("❌ UYARI: Hiçbir kanal bulunamadı, işlem sonlandırılıyor.")
            browser.close()
            sys.exit(1)
        
        m3u_content = []
        # Dosya adı kanallar2.m3u8 olarak ayarlandı.
        output_filename = "kanallar2.m3u8"
        print(f"\n📺 {len(channels)} kanal için M3U8 linkleri işleniyor...")
        created = 0
        
        # --- YENİ EKLENEN KISIM: GLOBAL BAŞLIKLARI AYARLA ---
        # Tüm kanallar aynı kaynağı kullandığı için ilk kanaldan bilgiyi al
        player_origin_host = channels[0]['origin']
        player_referer = player_origin_host + '/' # Sonuna / ekle
        
        m3u_header_lines = [
            "#EXTM3U",
            f"#EXT-X-USER-AGENT:{USER_AGENT}",
            f"#EXT-X-REFERER:{player_referer}",
            f"#EXT-X-ORIGIN:{player_origin_host}"
        ]
        # --- BİTTİ ---
        
        for i, channel_info in enumerate(channels, 1):
            channel_name = channel_info['name']
            player_url = channel_info['url']
            group_name = channel_info['group']
            
            print(f"[{i}/{len(channels)}] {channel_name} (Grup: {group_name}) işleniyor...", end="")
            
            m3u8_link = extract_m3u8_from_page(page, player_url)
            
            if m3u8_link:
                print(" -> ✅ Link bulundu.")
                m3u_content.append(f'#EXTINF:-1 tvg-name="{channel_name}" group-title="{group_name}",{channel_name}')
                
                # --- KALDIRILDI ---
                # #EXTVLCOPT satırı artık burada KULLANILMAYACAK.
                
                m3u_content.append(m3u8_link)
                created += 1
            else:
                print(" -> ❌ Link bulunamadı.")
        
        browser.close()

        if created > 0:
            with open(output_filename, "w", encoding="utf-8") as f:
                # Önce global başlıkları yaz
                f.write("\n".join(m3u_header_lines))
                f.write("\n") 
                # Sonra kanal listesini yaz
                f.write("\n".join(m3u_content))
            print(f"\n\n📂 {created} kanal başarıyla '{output_filename}' dosyasına kaydedildi.")
        else:
            print("\n\nℹ️  Geçerli hiçbir M3U8 linki bulunamadığı için dosya oluşturulmadı.")

        print("\n" + "="*50)
        print("📊 İŞLEM SONUCLARI")
        print("="*50)
        print(f"✅ Başarıyla oluşturulan link: {created}")
        print(f"❌ Başarısız veya atlanan kanal: {len(channels) - created}")
        print("\n🎉 İşlem başarıyla tamamlandı!")

if __name__ == "__main__":
    main()
