import re
from typing import Dict, List, Optional, Tuple

from app.utils.singleton import Singleton


class StreamingPlatforms(metaclass=Singleton):
    """
    流媒体平台简称与全称。
    """
    STREAMING_PLATFORMS: List[Tuple[str, str]] = [
        ("AMZN", "Amazon"),
        ("NF", "Netflix"),
        ("ATVP", "Apple TV+"),
        ("iT", "iTunes"),
        ("DSNP", "Disney+"),
        ("HS", "Hotstar"),
        ("APPS", "Disney+ MENA"),
        ("PMTP", "Paramount+"),
        ("HMAX", "Max"),
        ("", "Max"),
        ("HULU", "Hulu Networks"),
        ("MA", "Movies Anywhere"),
        ("BCORE", "Bravia Core"),
        ("CORE", "Bravia Core"),
        ("MS", "Microsoft Store"),
        ("SHO", "Showtime"),
        ("STAN", "Stan"),
        ("PCOK", "Peacock"),
        ("SKST", "SkyShowtime"),
        ("NOW", "Now"),
        ("FXTL", "Foxtel Now"),
        ("BNGE", "Binge"),
        ("CRKL", "Crackle"),
        ("RKTN", "Rakuten TV"),
        ("ALL4", "Channel 4"),
        ("AS", "Adult Swim"),
        ("BRTB", "Brtb TV"),
        ("CNLP", "Canal+"),
        ("CRIT", "Criterion Channel"),
        ("DSCP", "Discovery+"),
        ("FOOD", "Food Network"),
        ("MUBI", "Mubi"),
        ("PLAY", "Google Play"),
        ("YT", "YouTube"),
        ("", "friDay"),
        ("", "KKTV"),
        ("", "ofiii"),
        ("", "LiTV"),
        ("", "MyVideo"),
        ("Hami", "Hami Video"),
        ("HamiVideo", "Hami Video"),
        ("MW", "meWATCH"),
        ("CATCHPLAY", "CATCHPLAY+"),
        ("CPP", "CATCHPLAY+"),
        ("LINETV", "LINE TV"),
        ("VIU", "Viu"),
        ("iQIYI", "iQIYI"),
        ("IQ", "iQIYI"),
        ("TX", "TX"),
        ("", "WeTV"),
        ("YOUKU", "YOUKU"),
        ("YK", "YOUKU"),
        ("Bilibili", "Bilibili"),
        ("Bili", "Bilibili"),
        ("BLBL", "Bilibili"),
        ("MGTV", "MGTV"),
        ("MIGU", "MIGU"),
        ("LETV", "LETV"),
        ("SOHU", "SOHU"),
        ("CCTV", "CCTV"),
        ("ABMA", "Abema"),
        ("ADN", ""),
        ("AT-X", ""),
        ("Baha", ""),
        ("BG", "B-Global"),
        ("CR", "Crunchyroll"),
        ("", "DMM"),
        ("FOD", ""),
        ("FUNi", "Funimation"),
        ("HIDI", "HIDIVE"),
        ("UNXT", "U-NEXT"),
        ("FAA", "Filmarchiv Austria"),
        ("CC", "Comedy Central"),
        ("iP", "BBC iPlayer"),
        ("9NOW", "9Now"),
        ("ABC", ""),
        ("", "AMC"),
        ("", "ZEE5"),
        ("", "WAVO"),
        ("SHAHID", "Shahid"),
        ("Flixole", "FlixOlé"),
        ("TOU", "Ici TOU.TV"),
        ("ROKU", "Roku"),
        ("KNPY", "Kanopy"),
        ("SNXT", "Sun NXT"),
        ("CUR", "Curiosity Stream"),
        ("MY5", "Channel 5"),
        ("AHA", "aha"),
        ("WOWP", "WOW Presents Plus"),
        ("JC", "JioCinema"),
        ("", "Dekkoo"),
        ("FILMZIE", "Filmzie"),
        ("HoiChoi", "Hoichoi"),
        ("VIKI", "Rakuten Viki"),
        ("SF", "SF Anytime"),
        ("PLEX", "Plex"),
        ("SHDR", "Shudder"),
        ("CRAV", "Crave"),
        ("CPE", "Cineplex Entertainment"),
        ("JF HC", ""),
        ("JF", ""),
        ("JFFP", ""),
        ("VIAP", "Viaplay"),
        ("TUBI", "TubiTV"),
        ("", "PBS"),
        ("PBSK", "PBS KIDS"),
        ("LGP", "Lionsgate Play"),
        ("", "CTV"),
        ("", "Cineverse"),
        ("LN", "Love Nature"),
        ("MP", "Movistar Plus+"),
        ("RUNTIME", "Runtime"),
        ("STZ", "STARZ"),
        ("FUBO", "fuboTV"),
        ("TENK", "Tënk"),
        ("KNOW", "Knowledge Network"),
        ("TVO", "tvo"),
        ("", "OVID"),
        ("CBC", "CBC Gem"),
        ("FANDOR", "fandor"),
        ("CW", "The CW"),
        ("KNPY", "Kanopy"),
        ("FREE", "Freeform"),
        ("AE", "A&E"),
        ("LIFE", "Lifetime"),
        ("WWEN", "WWE Network"),
        ("CMAX", "Cinemax"),
        ("HLMK", "Hallmark"),
        ("BYU", "BYUtv"),
        ("", "ViX"),
        ("VICE", "Viceland"),
        ("", "TVING"),
        ("USAN", "USA Network"),
        ("FOX", ""),
        ("", "TCM"),
        ("BRAV", "BravoTV"),
        ("", "TNT"),
        ("", "ZDF"),
        ("", "IndieFlix"),
        ("", "TLC"),
        ("", "HGTV"),
        ("ANPL", "Animal Planet"),
        ("TRVL", "Travel Channel"),
        ("", "VH1"),
        ("SAINA", "Saina Play"),
        ("SP", "Saina Play"),
        ("OXGN", "Oxygen"),
        ("PSN", "PlayStation Network"),
        ("PMNT", "Paramount Network"),
        ("FAWESOME", "Fawesome"),
        ("KLASSIKI", "Klassiki"),
        ("STRP", "Star+"),
        ("NATG", "National Geographic"),
        ("REVEEL", "Reveel"),
        ("FYI", "FYI Network"),
        ("WatchiT", "WATCH IT"),
        ("ITVX", "ITV"),
        ("GAIA", "Gaia"),
        ("", "FlixLatino"),
        ("CNNP", "CNN+"),
        ("TROMA", "Troma"),
        ("IVI", "Ivi"),
        ("9NOW", "9Now"),
        ("A3P", "Atresplayer"),
        ("7PLUS", "7plus"),
        ("", "SBS"),
        ("TEN", "10Play"),
        ("AUBC", ""),
        ("DSNY", "Disney Networks"),
        ("OSN", "OSN+"),
        ("SVT", "Sveriges Television"),
        ("LACINETEK", "LaCinetek"),
        ("", "Maxdome"),
        ("RTL", "RTL+"),
        ("ARTE", "Arte"),
        ("JOYN", "Joyn"),
        ("TV2", "TV 2"),
        ("3SAT", "3sat"),
        ("FILMINGO", "filmingo"),
        ("", "WOW"),
        ("OKKO", "Okko"),
        ("", "Go3"),
        ("ARGP", "Argo"),
        ("VOYO", "Voyo"),
        ("VMAX", "vivamax"),
        ("FILMIN", "Filmin"),
        ("", "Mitele"),
        ("MY5", "Channel 5"),
        ("", "ARD"),
        ("BK", "Bentkey"),
        ("BOOM", "Boomerang"),
        ("", "CBS"),
        ("CLBI", "Club illico"),
        ("CMOR", "C More"),
        ("CMT", ""),
        ("", "CNBC"),
        ("COOK", "Cooking Channel"),
        ("CWS", "CW Seed"),
        ("DCU", "DC Universe"),
        ("DDY", "Digiturk Dilediğin Yerde"),
        ("DEST", "Destination America"),
        ("DISC", "Discovery Channel"),
        ("DW", "DailyWire+"),
        ("DLWP", "DailyWire+"),
        ("DPLY", "dplay"),
        ("DRPO", "Dropout"),
        ("EPIX", "EPIX MGM+"),
        ("ESQ", "Esquire"),
        ("ETV", "E!"),
        ("FBWatch", "Facebook Watch"),
        ("FPT", "FPT Play"),
        ("FTV", "France.tv"),
        ("GLOB", "GloboSat Play"),
        ("GLBO", "Globoplay"),
        ("GO90", "go90"),
        ("HIST", "History Channel"),
        ("HPLAY", "Hungama Play"),
        ("KS", "Kaleidescape"),
        ("", "MBC"),
        ("MMAX", "ManoramaMAX"),
        ("MNBC", "MSNBC"),
        ("MTOD", "Motor Trend OnDemand"),
        ("NBC", ""),
        ("NBLA", "Nebula"),
        ("NICK", "Nickelodeon"),
        ("ODK", "OnDemandKorea"),
        ("POGO", "PokerGO"),
        ("PUHU", "puhutv"),
        ("QIBI", "Quibi"),
        ("RTE", "RTÉ"),
        ("SESO", "Seeso"),
        ("SPIK", "Spike"),
        ("SS", "Simply South"),
        ("SYFY", "SyFy"),
        ("TIMV", "TIMvision"),
        ("TK", "Tentkotta"),
        ("", "TV4"),
        ("TVL", "TV Land"),
        ("", "TVNZ"),
        ("", "UKTV"),
        ("VLCT", "Discovery Velocity"),
        ("VMEO", "Vimeo"),
        ("VRV", "VRV Defunct"),
        ("WTCH", "Watcha"),
        ("", "NowPlayer"),
        ("HuluJP", "Hulu Networks"),
        ("Gaga", "GagaOOLala"),
        ("MyTVS", "MyTVSuper"),
        ("", "BBC"),
        ("CC", "Comedy Central"),
        ("NowE", "Now E"),
        ("WAVVE", "Wavve"),
        ("SE", ""),
        ("", "BritBox"),
        ("AOD", "Anime on Demand"),
        ("AF", ""),
        ("BCH", "Bandai Channel"),
        ("VMJ", "VideoMarket"),
        ("LFTL", "Laftel"),
        ("WAKA", "Wakanim"),
        ("WAKANIM", "Wakanim"),
        ("AO", "AnimeOnegai"),
        ("", "Lemino"),
        ("VIDIO", "Vidio"),
        ("TVER", "TVer"),
        ("", "MBS"),
        ("LFTLNET", "Laftel"),
        ("JONU", "Jonu Play"),
        ("PlutoTV", "Pluto TV"),
        ("AbemaTV", "Abema"),
        ("", "dTV"),
        ("NYMEY", "Nymey"),
        ("SMNS", "SAMANSA"),
        ("CTHP", "CATCHPLAY+"),
        ("HBOGO", "HBO GO"),
        ("HBO", "HBO"),
        ("FPTP", "FPT Play"),
        ("", "LOCIPO"),
        ("DANT", "DANET"),
        ("OV", "OceanVeil"),
    ]

    def __init__(self):
        """初始化流媒体平台匹配器"""
        self._lookup_cache: Dict[str, str] = {}
        self._code_cache: Dict[str, str] = {}
        self._aliases_cache: Dict[str, List[str]] = {}
        self._build_cache()

    def _build_cache(self) -> None:
        """
        构建查询缓存。
        """
        self._lookup_cache.clear()
        self._code_cache.clear()
        self._aliases_cache.clear()
        for short_name, full_name in self.STREAMING_PLATFORMS:
            canonical_name = full_name or short_name
            if not canonical_name:
                continue

            preferred_code = short_name or full_name
            canonical_key = canonical_name.upper()
            self._code_cache.setdefault(canonical_key, preferred_code)
            if short_name:
                self._code_cache[short_name.upper()] = short_name

            alias_list = self._aliases_cache.setdefault(canonical_key, [])
            for alias in (short_name, full_name):
                if alias:
                    self._lookup_cache[alias.upper()] = canonical_name
                    if alias not in alias_list:
                        alias_list.append(alias)

    def get_lookup_cache(self) -> dict:
        """
        返回流媒体平台查询表副本，供批量解析配置复用。
        """
        return dict(self._lookup_cache)

    def get_streaming_platform_name(self, platform_code: str) -> Optional[str]:
        """
        根据流媒体平台简称或全称获取标准名称。
        """
        if platform_code is None:
            return None
        return self._lookup_cache.get(platform_code.upper())

    def get_streaming_platform_code(
        self,
        platform_name_or_code: Optional[str],
        title: Optional[str] = None,
    ) -> Optional[str]:
        """
        将识别出的流媒体平台名称或简称还原为种子标题中使用的短标识（如 iQIYI、TX、NF、DSNP）。
        """
        if not platform_name_or_code:
            return None
        raw = str(platform_name_or_code).strip()
        if not raw:
            return None
        canonical = self._lookup_cache.get(raw.upper(), raw)
        if title:
            aliases = self._aliases_cache.get(canonical.upper(), [raw])
            for alias in aliases:
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", title, re.IGNORECASE):
                    return alias
        return self._code_cache.get(raw.upper(), raw)

    def is_streaming_platform(self, name: str) -> bool:
        """
        判断给定的字符串是否为已知的流媒体平台代码或名称。
        """
        if name is None:
            return False
        return name.upper() in self._lookup_cache
