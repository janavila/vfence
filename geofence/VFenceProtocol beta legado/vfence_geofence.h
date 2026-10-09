#pragma once

#include <stddef.h>
#include <stdint.h>

#include "vfence_protocol.h"

namespace vfence {

enum class ProximityState : uint8_t {
  FAR = 0,
  NEAR = 1,
  VERY_NEAR = 2
};

struct GeofenceResult {
  bool valid;
  bool inside;
  double distanceBorderM;
  ProximityState proximity;
};

class GeofenceEngine {
 public:
  GeofenceEngine();

  bool setFence(const CoordinateE7* points, uint8_t count, uint16_t fenceVersion);
  bool setThresholds(double veryNearM, double nearM);

  GeofenceResult analyze(const CoordinateE7& position) const;

  uint8_t vertexCount() const { return count_; }
  uint16_t fenceVersion() const { return fenceVersion_; }
  bool valid() const { return valid_; }

 private:
  struct XY {
    double x;
    double y;
  };

  CoordinateE7 geo_[MAX_VERTICES];
  XY xy_[MAX_VERTICES];
  uint8_t count_;
  uint16_t fenceVersion_;
  bool valid_;
  double veryNearM_;
  double nearM_;
  double latRefRad_;
  double lonRefRad_;
  double metersPerRadLat_;
  double metersPerRadLon_;

  XY toXY(const CoordinateE7& p) const;
  bool pointInside(const XY& p) const;
  double distanceToBorder(const XY& p) const;
  static double pointSegmentDistance(const XY& p, const XY& a, const XY& b);
};

} // namespace vfence
