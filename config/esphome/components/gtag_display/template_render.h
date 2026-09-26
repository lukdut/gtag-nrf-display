#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include "template_font_v1.h"
#include "template_font_large_v1.h"
#include "template_clock_v1.h"

namespace esphome::gtag_display::template_render {

// Codec 2 promises exactly template 1/revision 1, including all font metrics/pixels.
// Codec 3 adds clock (2), clock/two values (3), and single value (4), revision 1.
// Keep the existing revisions immutable when introducing future renderers.
constexpr uint8_t TEMPLATE_ID = 1;
constexpr uint8_t REVISION = 1;
constexpr size_t MAX_CHARS = 256;
inline uint16_t u16(const uint8_t *p) { return uint16_t(p[0]) | (uint16_t(p[1]) << 8); }
inline uint32_t u32(const uint8_t *p) { return u16(p) | (uint32_t(u16(p + 2)) << 16); }
inline int signed_byte(uint8_t b) { return b < 128 ? int(b) : int(b) - 256; }
inline int signed_word(const uint8_t *p) { const auto v = u16(p); return v < 32768 ? int(v) : int(v) - 65536; }

struct Font {
  const uint8_t *data{template_font_v1::DATA};
  uint16_t count{u16(data + 10)};
  const uint8_t *glyphs{nullptr};
  const uint8_t *pairs{nullptr};
  uint32_t curves_offset{template_font_v1::KERN_CURVES_OFFSET};
  uint8_t sizes{23};
  bool sparse{false};
  uint8_t size_index;
  explicit Font(uint8_t size) : size_index(size - 8) {
    if (size > 30) {
      if (size != 44 && size != 46) return;
      data = template_font_large_v1::DATA;
      curves_offset = template_font_large_v1::KERN_CURVES_OFFSET;
      size_index = size - 44;
      sizes = 3;
      sparse = true;
    }
    const auto *entry = data + 12 + count * 2 + size_index * 8;
    glyphs = data + u32(entry);
    pairs = data + u32(entry + 4);
  }
  int find(uint32_t cp) const {
    size_t lo = 0, hi = count;
    while (lo < hi) {
      const size_t mid = (lo + hi) / 2;
      if (u16(data + 12 + mid * 2) < cp) lo = mid + 1;
      else hi = mid;
    }
    return lo < count && u16(data + 12 + lo * 2) == cp ? int(lo) : -1;
  }
  const uint8_t *glyph(uint16_t index) const {
    static constexpr uint8_t missing[10] = {0, 0, 0, 0, 0, 0, 0, 0, 255, 255};
    if (!glyphs) return missing;
    if (!sparse) return glyphs + index * 10;
    size_t lo = 0, hi = u16(glyphs);
    while (lo < hi) {
      const size_t mid = (lo + hi) / 2;
      if (u16(glyphs + 2 + mid * 12) < index) lo = mid + 1;
      else hi = mid;
    }
    const auto *record = glyphs + 2 + lo * 12;
    return lo < u16(glyphs) && u16(record) == index ? record + 2 : missing;
  }
  // Only immutable, generated font bytes use this bounded per-glyph decoder.
  const uint8_t *bitmap(const uint8_t *glyph, uint8_t *scratch, size_t capacity) const {
    const uint32_t offset = u32(glyph);
    const uint8_t *input = data + (offset & 0x7FFFFFFFU);
    if (!(offset & 0x80000000U)) return input;
    const size_t length = (size_t(glyph[4]) * glyph[5] + 7) / 8;
    if (length > capacity) return nullptr;
    size_t pos = 0;
    while (pos < length) {
      const uint8_t flags = *input++;
      for (unsigned bit = 0; bit < 8 && pos < length; ++bit) {
        if (flags & (1U << bit)) {
          const size_t distance = *input++, count = *input++;
          if (!distance || distance > pos || count < 3 || count > length - pos) return nullptr;
          for (size_t i = 0; i < count; ++i, ++pos) scratch[pos] = scratch[pos - distance];
        } else scratch[pos++] = *input++;
      }
    }
    return scratch;
  }
  int kern(uint16_t first, uint16_t second) const {
    const uint32_t key = uint32_t(first) * count + second;
    size_t lo = 0, hi = u16(pairs);
    while (lo < hi) {
      const size_t mid = (lo + hi) / 2;
      if (u32(pairs + 2 + mid * 6) < key) lo = mid + 1;
      else hi = mid;
    }
    const auto *pair = pairs + 2 + lo * 6;
    if (lo == u16(pairs) || u32(pair) != key) return 0;
    return signed_word(data + curves_offset + (u16(pair + 4) * sizes + size_index) * 2);
  }
  int measure(const uint16_t *indexes, size_t length) const {
    int result = 0;
    for (size_t i = 0; i < length; ++i) {
      result += u16(glyph(indexes[i]) + 8);
      if (i) result += kern(indexes[i - 1], indexes[i]);
    }
    return result;
  }
};

inline bool utf8(const uint8_t *text, size_t length, uint16_t *indexes, size_t &count) {
  count = 0;
  const Font font(8);
  for (size_t offset = 0; offset < length;) {
    uint32_t cp = text[offset++];
    size_t extra = 0;
    uint32_t minimum = 0;
    if (cp >= 0xC2 && cp <= 0xDF) { cp &= 0x1F; extra = 1; minimum = 0x80; }
    else if (cp >= 0xE0 && cp <= 0xEF) { cp &= 0x0F; extra = 2; minimum = 0x800; }
    else if (cp >= 0xF0 && cp <= 0xF4) { cp &= 7; extra = 3; minimum = 0x10000; }
    else if (cp >= 0x80) return false;
    if (extra > length - offset) return false;
    while (extra--) {
      const uint8_t byte = text[offset++];
      if ((byte & 0xC0) != 0x80) return false;
      cp = (cp << 6) | (byte & 0x3F);
    }
    if (cp < minimum || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
    const int index = font.find(cp);
    if (index < 0 || count == MAX_CHARS) return false;
    indexes[count++] = uint16_t(index);
  }
  return true;
}

inline void pixel(uint8_t *raw, int x, int y) {
  if (x >= 0 && x < 256 && y >= 0 && y < 128) raw[y * 32 + x / 8] &= ~(1U << (x % 8));
}

enum Align : uint8_t { LEFT, CENTER, RIGHT };
struct Slot { int x; int y; uint8_t size; int width; Align align; };
constexpr Slot SLOTS[] = {{128, 13, 28, 240, CENTER}, {8, 54, 16, 112, LEFT},
                         {140, 54, 16, 108, LEFT}, {8, 84, 30, 112, LEFT}, {140, 84, 30, 108, LEFT}};
constexpr Slot CLOCK[] = {{248, 10, 16, 0, RIGHT}, {128, 44, 46, 0, CENTER}, {128, 103, 18, 0, CENTER}};
constexpr Slot CLOCK_TWO[] = {{8, 13, 28, 112, LEFT}, {248, 23, 16, 122, RIGHT},
                             SLOTS[1], SLOTS[2], SLOTS[3], SLOTS[4]};
constexpr Slot SINGLE[] = {{128, 10, 20, 240, CENTER}, {128, 64, 44, 240, CENTER}};
constexpr uint8_t CLOCK_ICON[] = {
  0x00,0x00,0x00,0xf8,0x01,0x60,0x60,0x00,0x01,0x08,0x08,0x04,0x41,0x40,0x20,0x04,0x04,
  0x22,0x40,0x40,0x02,0x04,0x24,0x40,0x40,0x02,0x04,0x24,0x80,0x41,0x02,0x20,0x44,0x00,
  0x20,0x04,0x00,0x82,0x00,0x10,0x10,0x80,0x00,0x06,0x06,0x80,0x1f,0x00,0x00,0x00};

inline bool text(uint8_t *raw, const uint8_t *value, size_t length, Slot slot) {
  // One spare entry for the ellipsis. This and metrics remain on the stack;
  // the immutable font stays in flash and no dynamic allocation is needed.
  std::array<uint16_t, MAX_CHARS + 1> indexes{};
  size_t count = 0;
  if (!utf8(value, length, indexes.data(), count)) return false;
  while (true) {
    const Font candidate(slot.size);
    if (!candidate.glyphs) return false;
    for (size_t i = 0; i < count; ++i)
      if (u16(candidate.glyph(indexes[i]) + 8) == 65535) return false;
    if (!slot.width || slot.size == 8 || candidate.measure(indexes.data(), count) <= slot.width * 64) break;
    --slot.size;
  }
  const Font font(slot.size);
  int advance = font.measure(indexes.data(), count);
  if (slot.width && advance > slot.width * 64) {
    const auto ellipsis = uint16_t(font.find(0x2026));
    while (true) {
      indexes[count] = ellipsis;
      advance = font.measure(indexes.data(), count + 1);
      if (advance <= slot.width * 64 || !count) break;
      --count;
    }
    ++count;
  }
  if (slot.align == CENTER) slot.x -= (advance + 64) / 128;
  else if (slot.align == RIGHT) slot.x -= (advance + 32) / 64;
  int top = 127;
  for (size_t i = 0; i < count; ++i) {
    const auto *glyph = font.glyph(indexes[i]);
    if (glyph[5] && signed_byte(glyph[7]) < top) top = signed_byte(glyph[7]);
  }
  if (top == 127) top = 0;
  int pen = 0;
  std::array<uint8_t, 512> scratch{};
  for (size_t i = 0; i < count; ++i) {
    const auto *glyph = font.glyph(indexes[i]);
    const auto *bitmap = font.bitmap(glyph, scratch.data(), scratch.size());
    if (!bitmap) return false;
    const int width = glyph[4], height = glyph[5];
    const int origin = slot.x + (pen + 32) / 64 + signed_byte(glyph[6]);
    for (int row = 0; row < height; ++row)
      for (int column = 0; column < width; ++column) {
        const int bit = row * width + column;
        if (bitmap[bit / 8] & (1U << (bit % 8)))
          pixel(raw, origin + column, slot.y + signed_byte(glyph[7]) - top + row);
      }
    pen += u16(glyph + 8);
    if (i + 1 < count) pen += font.kern(indexes[i], indexes[i + 1]);
  }
  return true;
}

inline bool render(const uint8_t *payload, size_t length, uint8_t *raw, size_t raw_size) {
  if (!payload || !raw || raw_size != 4096 || length < 2 || length > 4096 ||
      payload[0] < 1 || payload[0] > 4 || payload[1] != REVISION) return false;
  const uint8_t id = payload[0];
  const size_t fields = id == 1 ? 5 : id == 2 ? 3 : id == 3 ? 6 : 2;
  const Slot *slots = id == 1 ? SLOTS : id == 2 ? CLOCK : id == 3 ? CLOCK_TWO : SINGLE;
  size_t positions[6], lengths[6];
  size_t offset = 2;
  for (size_t i = 0; i < fields; ++i) {
    if (offset + 2 > length) return false;
    lengths[i] = u16(payload + offset);
    offset += 2;
    if (lengths[i] > 1024 || lengths[i] > length - offset) return false;
    positions[i] = offset;
    offset += lengths[i];
  }
  if (offset != length) return false;
  std::memset(raw, 0xFF, raw_size);
  for (int x = 8; x <= 247; ++x) pixel(raw, x, id == 2 ? 35 : id == 4 ? 38 : 42);
  if (id == 1 || id == 3)
    for (int y = 53; y <= 119; ++y) pixel(raw, 128, y);
  if (id == 2) {
    for (int bit = 0; bit < 400; ++bit)
      if (CLOCK_ICON[bit / 8] & (1U << (bit % 8))) pixel(raw, 8 + bit % 20, 8 + bit / 20);
    const char label[] = "СЕЙЧАС";
    if (!text(raw, reinterpret_cast<const uint8_t *>(label), sizeof(label) - 1, {36, 10, 16, 0, LEFT})) return false;
    const char *weekdays[] = {"Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"};
    size_t day = 0;
    while (day < 7 && (lengths[0] != 4 || std::memcmp(payload + positions[0], weekdays[day], 4))) ++day;
    if (day == 7) return false;
    const auto *weekday = template_clock_v1::WEEKDAYS + day * 65;
    for (int bit = 0; bit < 520; ++bit)
      if (weekday[bit / 8] & (1U << (bit % 8))) pixel(raw, 222 + bit % 26, 10 + bit / 26);
  }
  for (size_t i = id == 2 ? 1 : 0; i < fields; ++i)
    if (!text(raw, payload + positions[i], lengths[i], slots[i])) return false;
  return true;
}
}  // namespace esphome::gtag_display::template_render
