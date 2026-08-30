"""Small local seed mappings for current QuantDesk observation samples.

These rows are intentionally narrow. They are only used to prepare an import
draft for the user's current watch/portfolio samples when external providers
are unavailable; unknown symbols stay pending_manual instead of being guessed.
"""

from __future__ import annotations

THEME_MAP_SEED_ROWS: list[dict[str, str]] = [
    {
        "symbol": "600744",
        "name": "华银电力",
        "sector": "公用事业",
        "industry": "电力",
        "concept_tags": "电力;绿色电力",
        "theme_tags": "电力;绿色电力",
        "primary_theme": "绿色电力",
        "source": "seed",
        "notes": "本地种子映射，仅用于补足当前观察样本。",
    },
    {
        "symbol": "600958",
        "name": "东方证券",
        "sector": "非银金融",
        "industry": "证券",
        "concept_tags": "券商;金融",
        "theme_tags": "券商;金融",
        "primary_theme": "券商金融",
        "source": "seed",
        "notes": "本地种子映射，仅用于补足当前观察样本。",
    },
    {
        "symbol": "002115",
        "name": "三维通信",
        "sector": "通信",
        "industry": "通信设备",
        "concept_tags": "通信;5G",
        "theme_tags": "通信;5G",
        "primary_theme": "通信5G",
        "source": "seed",
        "notes": "本地种子映射，仅用于补足当前观察样本。",
    },
    {
        "symbol": "300323",
        "name": "华灿光电",
        "sector": "电子",
        "industry": "光学光电子",
        "concept_tags": "LED;MiniLED",
        "theme_tags": "LED;MiniLED",
        "primary_theme": "LED",
        "source": "seed",
        "notes": "本地种子映射，仅用于补足当前观察样本。",
    },
    {
        "symbol": "601975",
        "name": "招商南油",
        "sector": "交通运输",
        "industry": "航运港口",
        "concept_tags": "航运;油运",
        "theme_tags": "航运;油运",
        "primary_theme": "油运",
        "source": "seed",
        "notes": "本地种子映射，仅用于补足当前观察样本。",
    },
]
