# Steam Workshop Downloader

Steam Workshop koleksiyonlarını veya tek bir modu SteamCMD ile indiren küçük bir Windows uygulaması. ID'leri ve hedef klasörü giriyorsun, indirme durumunu arayüzden takip ediyorsun.

## Kurulum

- Windows 10 veya 11
- Python 3.11 veya üzeri (kurarken Tkinter ve PATH seçeneklerini açık bırak)
- [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD)

Projeyi indirip klasöründe terminal aç:

```powershell
py -m pip install -r requirements.txt
py steam_downloader.py
```

Konsol görünmesin istersen `launch.vbs` dosyasını kullanabilirsin. Bunun için `pythonw` komutunun PATH üzerinde bulunması gerekiyor.

## Kullanım

1. Oyunun App ID'sini gir veya listeden seç.
2. Koleksiyon ID'sini yaz. Tek mod için ilgili kutuyu işaretleyip mod ID'sini gir.
3. Hedef klasörü ve `steamcmd.exe` dosyasını seç.
4. İndirmeyi başlat.

Workshop bağlantısındaki `?id=123456789` bölümünde bulunan sayı, modun veya koleksiyonun ID'si. Kutulara bağlantının tamamını değil, bu sayıyı yaz.

Modlar hedef klasörün içinde kendi ID'leriyle saklanır. Güncellemede eski mod klasörü yeni içerikle değiştirilir; mod klasörlerine eklediğin kişisel dosyaları başka yerde tut.

## Birkaç not

- Her oyun anonim indirmeye izin vermiyor. Bazıları Steam hesabı ve oyuna sahip olmanı istiyor. Oyun listesi, Workshop desteği garantisi değil; doğru App ID'yi kendin de girebilirsin.
- Steam Guard gibi etkileşimli girişler arayüzden cevaplanamıyor. Gerekirse önce SteamCMD'de elle giriş yapıp doğrulamayı tamamla. Hesapla indirme her oyun için canlı olarak denenmiş değil.
- İç içe koleksiyonlar desteklenmiyor. Alt koleksiyonları ayrı indir.
- SteamCMD önbelleği silinmiyor, bu yüzden modlar diskte fazladan yer kaplayabilir.
- Kapatma sırasında indirme durdurulur. API sorgusu sürüyorsa bağlantı zaman aşımına kadar bekleyebilir.

Son kullanılan ayarlar `settings.json` dosyasında tutulur. Şifre ve Steam Guard kodu bu dosyaya kaydedilmez; ancak girersen SteamCMD'ye komut satırı üzerinden aktarılır. Ayarlar ve hata günlükleri Git dışında tutulur.

Uygulama açılmazsa terminalden çalıştır veya aynı klasördeki `error.log` dosyasına bak. Mod güncellemesinde geri yükleme de engellenirse eski dosyaların bulunduğu yedek klasörün yolu günlükte gösterilir.

## Geliştirme

```powershell
py -m unittest discover -v
py -m py_compile steam_downloader.py test_downloader.py
```

Testler gerçek indirme yapmaz; dosya koruma, iptal, ilerleme ve API hata senaryolarını kontrol eder. Gerçek Steam girişi ve arayüz ayrıca elle denenmeli.

Bir sorun bildirirken ne yaptığını, beklediğin sonucu ve günlükteki ilgili satırları ekle. Kullanıcı adı, şifre veya Steam Guard kodunu paylaşma.

Steam ve Steam Workshop, Valve'ın markalarıdır. Bu proje Valve ile bağlantılı değildir.
