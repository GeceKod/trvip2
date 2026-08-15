import re
import sys
import time
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# Güncel adresi bulmak için kullanılacak portal adresi
PORTAL_DOMAIN = "https://www.selcuksportshd.is/"

# Global olarak kullanılacak User-Agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def find_working_domain(page):
  """Portal sayfasını ziyaret eder ve 'a.site-button' class'ına sahip

  elementin href özelliğinden güncel domain'i çeker.
  """
  print(f"\n🔎 Güncel domain {PORTAL_DOMAIN} adresinden alınıyor...")
  try:
    page.goto(PORTAL_DOMAIN, timeout=20000, wait_until="domcontentloaded")

    selector = 'a.site-button:has(img[alt="Site Giriş"])'

    page.wait_for_selector(selector, timeout=10000)
    link_element = page.query_selector(selector)

    if not link_element:
      print("-> ❌ Portal sayfasında 'Site Giriş' linki bulunamadı.")
      return None

    domain = link_element.get_attribute("href")

    if not domain:
      print("-> ❌ Link elementinde 'href' özelliği bulunamadı.")
      return None

    domain = domain.rstrip("/")
    print(f"✅ Güncel domain başarıyla bulundu: {domain}")
    return domain

  except Exception as e:
    print(
        "❌ Portal sayfasına ulaşılamadı veya domain alınamadı:"
        f" {e.__class__.__name__}"
    )
    return None


def get_channel_group(channel_name):
  """Verilen kanal ismine göre bir grup adı döndürür."""
  channel_name_lower = channel_name.lower()
  group_mappings = {
      "BeinSports": ["bein sports", "beın sports"],
      "S Sports": ["s sport"],
      "Tivibu": ["tivibu spor"],
      "Ulusal Kanallar": ["a spor", "trt spor", "trt 1"],
      "Diğer Spor": ["smart spor", "nba tv", "eurosport"],
      "Belgesel": [
          "national geographic",
          "nat geo",
          "discovery",
          "dmax",
          "bbc earth",
          "history",
      ],
      "Film & Dizi": ["bein series", "bein movies", "movie smart"],
  }
  for group, keywords in group_mappings.items():
    for keyword in keywords:
      if keyword in channel_name_lower:
        return group
  return "Maç Yayınları"


def scrape_channel_links(page, domain_to_scrape):
  """Selçuk Sports ana sayfasını ziyaret eder ve tüm kanalları

  isim, URL, grup ve GEREKLİ REFERER BİLGİSİ (origin) ile toplar.
  """
  print(f"\n📡 Kanallar {domain_to_scrape} adresinden çekiliyor...")
  channels = []
  try:
    page.goto(domain_to_scrape, timeout=25000, wait_until="domcontentloaded")

    link_elements = page.query_selector_all("a[data-url]")

    if not link_elements:
      print("❌ Ana sayfada 'data-url' içeren hiçbir kanal linki bulunamadı.")
      return []

    for link in link_elements:
      player_url = link.get_attribute("data-url")
      name_element = link.query_selector("div.name")

      if name_element and player_url:
        channel_name = name_element.inner_text().strip()

        if player_url.startswith("/"):
          base_domain = domain_to_scrape.rstrip("/")
          player_url = f"{base_domain}{player_url}"

        try:
          parsed_player_url = urlparse(player_url)
          player_origin = (
              f"{parsed_player_url.scheme}://{parsed_player_url.netloc}"
          )
        except Exception:
          player_origin = None

        if not player_origin:
          continue

        group_name = get_channel_group(channel_name)

        channels.append({
            "name": channel_name,
            "url": player_url,
            "group": group_name,
            "origin": player_origin,
        })

    print(
        f"✅ {len(channels)} adet potansiyel kanal linki bulundu ve"
        " gruplandırıldı."
    )
    return channels

  except PlaywrightError as e:
    print(
        f"❌ Selçuk Sports ana sayfasına ulaşılamadı. Hata: {e.__class__.__name__}"
    )
    return []


def extract_m3u8_from_page(page, player_url):
  """Oynatıcı sayfasından M3U8 linkini ağ isteklerini dinleyerek

  veya sayfa kaynağından doğru yayın kimliğini bularak oluşturur.
  """
  m3u8_url = None

  # Ağ isteklerini dinleyen fonksiyon
  def handle_request(request):
    nonlocal m3u8_url
    url = request.url
    # .m3u8 içeren ve stil / eklenti olmayan gerçek yayın linkini yakala
    if (
        ".m3u8" in url
        and not m3u8_url
        and "cl-levels" not in url
        and "style" not in url
    ):
      m3u8_url = url

  try:
    page.on("request", handle_request)
    page.goto(player_url, timeout=20000, wait_until="domcontentloaded")

    # Oynatıcının m3u8 isteğini yapması için kısa bir süre bekle
    try:
      page.wait_for_timeout(2500)
    except:
      pass

    page.remove_listener("request", handle_request)

    # 1. Öncelik: Ağ isteğinde yakalanan gerçek M3U8
    if m3u8_url:
      return m3u8_url

    # 2. Öncelik: Ağ isteği yakalanamazsa sayfa içeriğini analiz et
    content = page.content()

    # Sayfa içinde doğrudan tam playlist linki varsa (cl-levels içermeyen)
    m3u8_match = re.search(
        r"(https?://[^\s'\"<>]+playlist\.m3u8[^\s'\"<>]*)", content
    )
    if m3u8_match and "cl-levels" not in m3u8_match.group(1):
      return m3u8_match.group(1)

    # baseStreamUrl ara
    base_url_match = re.search(
        r"(?:this\.baseStreamUrl|baseStreamUrl)\s*[:=]\s*['\"](https?://.*?)['\"]",
        content,
    )
    if not base_url_match:
      print(" -> ❌ 'baseStreamUrl' bulunamadı.", end="")
      return None

    base_url = base_url_match.group(1)

    # HTML tag'lerindeki id="..." etiketine takılmamak için spesifik JS değişkenleri ara
    stream_id = None
    stream_id_match = re.search(
        r"(?:streamName|streamId|channelId|play_id|channel|stream)\s*[:=]\s*['\"]([^'\"]+)['\"]",
        content,
    )

    if stream_id_match:
      stream_id = stream_id_match.group(1)
    else:
      # Bulunamazsa URL'deki id veya channel parametresini al
      parsed_url = urlparse(player_url)
      query_params = parse_qs(parsed_url.query)
      stream_id = query_params.get("id", [None])[0] or query_params.get(
          "channel", [None]
      )[0]

    if not stream_id or "cl-levels" in stream_id:
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


def main():
  with sync_playwright() as p:
    print("🚀 Playwright ile Selçuk M3U8 Kanal İndirici Başlatılıyor...")

    # Autoplay kısıtlamasını kaldırarak m3u8 ağ isteklerinin anında tetiklenmesini sağla
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--autoplay-policy=no-user-gesture-required",
            "--disable-web-security",
        ],
    )
    context = browser.new_context(user_agent=USER_AGENT)
    page = context.new_page()

    selcuksports_domain = find_working_domain(page)

    if not selcuksports_domain:
      print(
          "❌ UYARI: Güncel domain portal sayfasından alınamadı. İşlem"
          " sonlandırılıyor."
      )
      browser.close()
      sys.exit(1)

    channels = scrape_channel_links(page, selcuksports_domain)

    if not channels:
      print("❌ UYARI: Hiçbir kanal bulunamadı, işlem sonlandırılıyor.")
      browser.close()
      sys.exit(1)

    m3u_content = []
    output_filename = "kanallar2.m3u8"
    print(f"\n📺 {len(channels)} kanal için M3U8 linkleri işleniyor...")
    created = 0

    # GLOBAL BAŞLIKLARI AYARLA
    player_origin_host = channels[0]["origin"]
    player_referer = player_origin_host + "/"

    m3u_header_lines = [
        "#EXTM3U",
        f"#EXT-X-USER-AGENT:{USER_AGENT}",
        f"#EXT-X-REFERER:{player_referer}",
        f"#EXT-X-ORIGIN:{player_origin_host}",
    ]

    for i, channel_info in enumerate(channels, 1):
      channel_name = channel_info["name"]
      player_url = channel_info["url"]
      group_name = channel_info["group"]

      print(
          f"[{i}/{len(channels)}] {channel_name} (Grup: {group_name})"
          " işleniyor...",
          end="",
      )

      m3u8_link = extract_m3u8_from_page(page, player_url)

      if m3u8_link:
        print(f" -> ✅ Link bulundu: {m3u8_link}")
        m3u_content.append(
            f'#EXTINF:-1 tvg-name="{channel_name}"'
            f' group-title="{group_name}",{channel_name}'
        )
        m3u_content.append(m3u8_link)
        created += 1
      else:
        print(" -> ❌ Link bulunamadı.")

    browser.close()

    if created > 0:
      with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_header_lines))
        f.write("\n")
        f.write("\n".join(m3u_content))
      print(
          f"\n\n📂 {created} kanal başarıyla '{output_filename}' dosyasına"
          " kaydedildi."
      )
    else:
      print(
          "\n\nℹ️  Geçerli hiçbir M3U8 linki bulunamadığı için dosya"
          " oluşturulmadı."
      )

    print("\n" + "=" * 50)
    print("📊 İŞLEM SONUCLARI")
    print("=" * 50)
    print(f"✅ Başarıyla oluşturulan link: {created}")
    print(f"❌ Başarısız veya atlanan kanal: {len(channels) - created}")
    print("\n🎉 İşlem başarıyla tamamlandı!")


if __name__ == "__main__":
  main()
