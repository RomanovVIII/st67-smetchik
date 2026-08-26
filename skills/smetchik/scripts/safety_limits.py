from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class XmlLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_depth: int = 128
    max_nodes: int = 200_000
    max_total_text_characters: int = 2_000_000
    max_single_text_characters: int = 200_000


@dataclass(frozen=True)
class PdfLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    max_pages: int = 2_000
    max_page_text_characters: int = 2_000_000
    max_total_text_characters: int = 20_000_000
    max_words_per_page: int = 200_000
    max_total_words: int = 1_000_000
    max_tables_per_page: int = 2_000
    max_total_table_cells: int = 1_000_000
    max_ocr_words_per_page: int = 100_000
    max_ocr_pages: int = 100
    default_ocr_dpi: int = 200
    min_ocr_dpi: int = 72
    max_render_dimension_pixels: int = 10_000
    max_render_pixels_per_page: int = 25_000_000
    max_total_render_pixels: int = 250_000_000
    max_rendered_image_bytes: int = 100 * 1024 * 1024


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 2_000
    max_declared_uncompressed_bytes: int = 500 * 1024 * 1024
    max_actual_uncompressed_bytes: int = 500 * 1024 * 1024
    max_single_uncompressed_bytes: int = 100 * 1024 * 1024
    max_compression_ratio: float = 200.0


@dataclass(frozen=True)
class InputDirectoryLimits:
    max_files: int = 10_000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_depth: int = 64


XML_LIMITS = XmlLimits()
PDF_LIMITS = PdfLimits()
ZIP_LIMITS = ZipLimits()
INPUT_DIRECTORY_LIMITS = InputDirectoryLimits()
