#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include "template_font_v1.h"

namespace esphome::gtag_display::template_render {

// Codec 2 promises exactly template 1/revision 1, including all font metrics/pixels.
// Keep the existing revision immutable when introducing future renderers.
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
  const uint8_t *glyphs;
  const uint8_t *pairs;
  uint8_t size_index;
  explicit Font(uint8_t size) : size_index(size - 8) {
    const auto *entry = data + 12 + count * 2 + (size - 8) * 8;
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
  const uint8_t *glyph(uint16_t index) const { return glyphs + index * 10; }
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
    return signed_word(data + template_font_v1::KERN_CURVES_OFFSET +
                       (u16(pair + 4) * 23 + size_index) * 2);
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

struct Slot { int x; int y; uint8_t size; int width; bool center; };
constexpr Slot SLOTS[] = {{128, 13, 28, 240, true}, {8, 54, 16, 112, false},
                         {140, 54, 16, 108, false}, {8, 84, 30, 112, false}, {140, 84, 30, 108, false}};

inline bool text(uint8_t *raw, const uint8_t *value, size_t length, Slot slot) {
  // One spare entry for the ellipsis. This and metrics remain on the stack;
  // the immutable font stays in flash and no dynamic allocation is needed.
  std::array<uint16_t, MAX_CHARS + 1> indexes{};
  size_t count = 0;
  if (!utf8(value, length, indexes.data(), count)) return false;
  while (slot.size > 8 && Font(slot.size).measure(indexes.data(), count) > slot.width * 64) --slot.size;
  const Font font(slot.size);
  int advance = font.measure(indexes.data(), count);
  if (advance > slot.width * 64) {
    const auto ellipsis = uint16_t(font.find(0x2026));
    while (true) {
      indexes[count] = ellipsis;
      advance = font.measure(indexes.data(), count + 1);
      if (advance <= slot.width * 64 || !count) break;
      --count;
    }
    ++count;
  }
  if (slot.center) slot.x -= (advance + 64) / 128;
  int top = 127;
  for (size_t i = 0; i < count; ++i) {
    const auto *glyph = font.glyph(indexes[i]);
    if (glyph[5] && signed_byte(glyph[7]) < top) top = signed_byte(glyph[7]);
  }
  if (top == 127) top = 0;
  int pen = 0;
  for (size_t i = 0; i < count; ++i) {
    const auto *glyph = font.glyph(indexes[i]);
    const auto *bitmap = font.data + u32(glyph);
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
  if (!payload || !raw || raw_size != 4096 || length < 12 || length > 4096 ||
      payload[0] != TEMPLATE_ID || payload[1] != REVISION) return false;
  size_t positions[5], lengths[5];
  size_t offset = 2;
  for (size_t i = 0; i < 5; ++i) {
    if (offset + 2 > length) return false;
    lengths[i] = u16(payload + offset);
    offset += 2;
    if (lengths[i] > 1024 || lengths[i] > length - offset) return false;
    positions[i] = offset;
    offset += lengths[i];
  }
  if (offset != length) return false;
  std::memset(raw, 0xFF, raw_size);
  for (int x = 8; x <= 247; ++x) pixel(raw, x, 42);
  for (int y = 53; y <= 119; ++y) pixel(raw, 128, y);
  for (size_t i = 0; i < 5; ++i)
    if (!text(raw, payload + positions[i], lengths[i], SLOTS[i])) return false;
  return true;
}
}  // namespace esphome::gtag_display::template_render
