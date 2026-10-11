#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile the production FOD state transitions with minimal HWC test doubles.

This host test executes the complete Z-order setters and the per-layer FOD
block from BuildLayerStack. It does not exercise Android buffers, SDM strategy,
DRM submission, fences, or panel hardware.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    body = source.index("{", start)
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


layers = (ROOT / "composer/hwc_layers.cpp").read_text()
display = (ROOT / "composer/hwc_display.cpp").read_text()
stack = function(display, "void HWCDisplay::BuildLayerStack()")
start = stack.index("#ifdef UDFPS_ZPOS")
end = stack.index("#endif", start) + len("#endif")
fod_block = stack[start:end]

harness = r"""
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <map>
#include <set>
#include <vector>
#ifndef TEST_NO_UDFPS
#define UDFPS_ZPOS
#endif
#define DLOGW(...) ((void)0)
#define DLOGE(...) ((void)0)
constexpr uint32_t FOD_PRESSED_LAYER_ZORDER = 0x20000000u;
using hwc2_layer_t = uint64_t;
namespace HWC2 { enum class Error { None, BadLayer }; }
struct Flags { bool fod_pressed = false; bool fod_pressed_present = false; };
struct Layer { Flags flags; };
struct Stack { Flags flags; };
class HWCLayer {
 public:
  enum { kZOrder = 1 };
  HWC2::Error SetLayerZOrder(uint32_t z);
  uint32_t GetZ() const { return z_; }
  bool IsFodPressed() const { return fod_pressed_; }
  uint32_t z_ = 0;
  bool fod_pressed_ = false;
  uint32_t geometry_changes_ = 0;
  Layer sdm_layer_;
};
struct ByZ {
  bool operator()(const HWCLayer* a, const HWCLayer* b) const {
    return a->GetZ() < b->GetZ();
  }
};
class HWCDisplay {
 public:
  HWC2::Error SetLayerZOrder(hwc2_layer_t layer_id, uint32_t z);
  uint64_t id_ = 0;
  std::map<hwc2_layer_t, HWCLayer*> layer_map_;
  std::multiset<HWCLayer*, ByZ> layer_set_;
};
"""
harness += function(layers, "HWC2::Error HWCLayer::SetLayerZOrder(uint32_t z)")
harness += function(display, "HWC2::Error HWCDisplay::SetLayerZOrder(")
harness += """
bool HasPressed(const std::vector<HWCLayer*>& layers) {
  Stack layer_stack_;
  for (auto hwc_layer : layers) {
    Layer* layer = &hwc_layer->sdm_layer_;
    layer->flags = {};
""" + fod_block + """
  }
  return layer_stack_.flags.fod_pressed_present;
}
"""
harness += r"""
int main() {
#ifndef TEST_NO_UDFPS
  constexpr auto pressed = FOD_PRESSED_LAYER_ZORDER;
  HWCLayer layer;
  layer.SetLayerZOrder(pressed | 5);
  assert(layer.GetZ() == 5 && layer.IsFodPressed());
  layer.geometry_changes_ = 0;
  layer.SetLayerZOrder(pressed | 5);
  assert(layer.GetZ() == 5 && layer.IsFodPressed());
  assert(layer.geometry_changes_ == 0);
  layer.SetLayerZOrder(pressed | 7);
  assert(layer.GetZ() == 7 && layer.IsFodPressed());
  layer.geometry_changes_ = 0;
  layer.SetLayerZOrder(7);
  assert(layer.GetZ() == 7 && !layer.IsFodPressed());
  assert(layer.geometry_changes_ & HWCLayer::kZOrder);
  layer.geometry_changes_ = 0;
  layer.SetLayerZOrder(7);
  assert(layer.geometry_changes_ == 0);
  std::puts("PASS: layer press, repeat, reorder, release, repeat release");

  HWCDisplay display;
  display.layer_map_[1] = &layer;
  display.layer_set_.insert(&layer);
  assert(display.SetLayerZOrder(1, pressed | 7) == HWC2::Error::None);
  layer.geometry_changes_ = 0;
  assert(display.SetLayerZOrder(1, pressed | 7) == HWC2::Error::None);
  assert(layer.GetZ() == 7 && layer.IsFodPressed());
  assert(layer.geometry_changes_ == 0 && display.layer_set_.size() == 1);
  assert(display.SetLayerZOrder(1, 7) == HWC2::Error::None);
  assert(!layer.IsFodPressed() && layer.GetZ() == 7);
  assert(display.SetLayerZOrder(99, 7) == HWC2::Error::BadLayer);
  display.layer_set_.clear();
  assert(display.SetLayerZOrder(1, 7) == HWC2::Error::BadLayer);
  std::puts("PASS: display repeat, same-Z release, missing ID, missing membership");

  HWCLayer ordinary, second;
  layer.SetLayerZOrder(pressed | 7);
  second.SetLayerZOrder(pressed | 9);
  assert(HasPressed({&layer, &ordinary}));
  assert(HasPressed({&ordinary, &layer}));
  assert(HasPressed({&layer, &ordinary, &second}));
  assert(layer.sdm_layer_.flags.fod_pressed);
  assert(!ordinary.sdm_layer_.flags.fod_pressed);
  assert(!HasPressed({&ordinary}));
  assert(!HasPressed({}));
  layer.SetLayerZOrder(7);
  assert(!HasPressed({&layer, &ordinary}));
  std::puts("PASS: stack ordering, multiple pressed layers, empty and released frames");
#else
  HWCLayer layer;
  layer.SetLayerZOrder(5);
  assert(layer.GetZ() == 5);
  layer.geometry_changes_ = 0;
  HWCDisplay display;
  display.layer_map_[1] = &layer;
  display.layer_set_.insert(&layer);
  assert(display.SetLayerZOrder(1, 5) == HWC2::Error::None);
  assert(layer.geometry_changes_ == 0);
  assert(display.SetLayerZOrder(1, 7) == HWC2::Error::None);
  assert(layer.GetZ() == 7);
  assert(!HasPressed({&layer}));
  std::puts("PASS: ordinary Z-order handling without UDFPS");
#endif
}
"""
with tempfile.TemporaryDirectory(prefix="qcom-fod-test-") as directory:
    source = Path(directory) / "fod_state_test.cpp"
    binary = Path(directory) / "fod_state_test"
    source.write_text(harness)
    for flags in ([], ["-DTEST_NO_UDFPS"]):
        subprocess.run(
            [os.environ.get("CXX", "c++"), "-std=c++17", "-Wall", "-Wextra",
             "-Werror", *flags, str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
