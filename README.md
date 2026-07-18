# TitanyumTPSX v2

Hostlada için çok sunuculu, kalıcı durumlu Minecraft sunucu izleyici Discord botu.

## Kurulum

```bash
pip install -r requirements.txt
cp config.json.example config.json
# config.json içini kendi bilgilerinizle doldurun
python3 titanyumtpsx.py
```

## config.json alanları

| Alan | Zorunlu | Açıklama |
|---|---|---|
| `token` | evet | Discord bot token |
| `guild_id` | evet | Slash komutların anında senkronize edileceği sunucu ID'si |
| `check_interval` | hayır | Saniye cinsinden kontrol aralığı (varsayılan 60, minimum 15) |
| `admin_role_ids` | hayır | `/giris`, `/ekle`, `/kaldir` komutlarını kullanabilecek rol ID'leri. Boş bırakılırsa yalnızca sunucu yöneticileri kullanabilir |
| `servers` | evet | İzlenecek sunucuların listesi — her biri `id`, `ip`, `name`, `channel_id` içerir |

Eski tek-sunucu formatındaki config'ler (`server_ip`, `servername`, `channel_id` doğrudan kökte) otomatik olarak yeni `servers` listesine dönüştürülür, elle taşımanıza gerek yok.

## Neler değişti / eklendi

**Düzeltilen hatalar**
- Eski mesaj temizleme kodu yanlış başlığı arıyordu (`"Minecraft Sunucu Durumu:"` vs gerçek başlık `"{isim} Sunucu Durumu: "`), hiçbir zaman eşleşmiyordu — düzeltildi.
- Ana döngü `on_ready` içine gömülüydü; bir hata döngüyü tamamen öldürüyordu. Artık `tasks.loop` ile çalışıyor ve çökerse 30 saniye sonra otomatik yeniden başlıyor.
- `giris_durum` bot yeniden başlatıldığında sıfırlanıyordu — artık `state.json`'a kalıcı olarak yazılıyor (atomik yazma ile, yarım dosya riski yok).

**Yeni özellikler**
- **Çoklu sunucu desteği** — `config.json`'a istediğiniz kadar sunucu ekleyebilir, her biri kendi kanalında ayrı embed ile takip edilir.
- **Slash komutlar**: `/durum`, `/gecmis`, `/giris`, `/ekle`, `/kaldir` — eski `!` prefix komutları yerine.
- **Yetki kontrolü** — sunucu ekleme/kaldırma ve giriş durumu değiştirme artık `admin_role_ids` veya sunucu yöneticiliği gerektiriyor.
- **Uptime yüzdesi ve sparkline** — son 40 kontrolün özet grafiği ve uptime% embed içinde gösteriliyor.
- **MOTD ve sürüm bilgisi** embed'e eklendi.
- **Rate-limit güvenliği** — sunucular arası bekleme ve 429 durumunda geri çekilme.
- **Dosyaya loglama** (`titanyumtpsx.log`, otomatik döngüsel/rotating) — artık sadece konsola değil.
- `message_content` intent'i kaldırıldı (artık gerekmiyor, slash komut kullanılıyor) — bot davetinde daha az yetki istemesi gerekir.

## Discord bot izinleri

Botu davet ederken en azından şu izinler gerekli: `Send Messages`, `Embed Links`, `Read Message History`, `Manage Messages` (eski mesajları temizlemek için), ve `applications.commands` scope'u (slash komutlar için).
