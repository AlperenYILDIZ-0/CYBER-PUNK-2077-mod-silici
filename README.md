# Cyberpunk 2077 Mod Bulucu ve Silici

Cyberpunk 2077 modlarını otomatik bulur, listeler ve seçtiklerini güvenle siler.
Finds, lists and safely removes Cyberpunk 2077 mods. (English summary below)

## Özellikler
- Oyun klasörünü otomatik bulur (Steam / GOG / Epic / Proton), bulamazsa elle seçtirir
- Mod adlarını, sayısını, dosya sayısını ve boyutunu gösterir
- Varsayılan olarak modları **yedeğe taşır** (geri yüklenebilir), isteğe bağlı kalıcı silme
- Silinen her şeyi geçmişe kaydeder (`gecmis.txt`)
- Altyapı modlarını (ArchiveXL, TweakXL, CET, RED4ext vb.) korur
- **Tam Karşılaştırma:** temiz kurulumun dosya listesini (temel) kaydeder, sonradan eklenen her şeyi bulur
- **Tam Sıfırla:** temelde olmayan her şeyi siler ve sonunda temiz olduğunu doğrular

## Kurulum
1. [Python 3.8+](https://www.python.org/downloads/) kur (ek paket gerekmez)
2. `cp2077_mod_silici.pyw` dosyasını indir ve **oyun klasörünün dışına** koy (ör. Masaüstü)
3. Çift tıkla

Metin modu: `python cp2077_mod_silici.pyw --cli`
Oyun yolunu elle vermek için: `python cp2077_mod_silici.pyw --yol "D:\Games\Cyberpunk 2077"`

## Gizlilik
Program **internete bağlanmaz** ve hiçbir veri göndermez. Geçmiş, ayar ve yedekler yalnızca
bilgisayarında, `~/CP2077_ModSilici/` klasöründe tutulur. Kaynak kod açıktır, kendin inceleyebilirsin.

## Önemli uyarılar
- **Kullanım riski size aittir.** Kullanmadan önce oyun kayıtlarınızı ve önemli dosyalarınızı yedekleyin.
- "Tam Sıfırla" temeli **modsuz, temiz kurulumda** aldıysan doğru çalışır. Temel mod kurulduktan sonra alınırsa o modlar özgün sayılır.
- "Tam Sıfırla" korumalı klasörlerdeki (ör. `archive/pc/patch`) sonradan eklenmiş dosyaları da siler.
- Oyun güncellenirse temeli yeniden oluştur veya Steam/GOG/Epic'ten "dosya bütünlüğünü doğrula" yap.
- Vortex kullanıyorsan modları Vortex'ten kaldırmak daha güvenlidir.

## English summary
A small Python/tkinter tool that scans a Cyberpunk 2077 install for mods, shows them with sizes,
and removes selected ones (moved to a backup folder by default, so they can be restored).
Core frameworks are protected. An optional baseline feature records a clean install and detects
anything added afterwards. The tool makes **no network connections**. Use at your own risk and
back up your saves first.

## Lisans
MIT License, bkz. [LICENSE](LICENSE)
