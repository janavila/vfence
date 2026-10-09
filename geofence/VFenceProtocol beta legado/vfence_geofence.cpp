#include "vfence_geofence.h"

#include <cmath>

namespace vfence {

static const double WGS84_A = 6378137.0;
static const double WGS84_E2 = 6.69437999014e-3;
static const double DEG_TO_RAD_D = 0.017453292519943295;

GeofenceEngine::GeofenceEngine()
    : count_(0), fenceVersion_(0), valid_(false),
      veryNearM_(10.0), nearM_(30.0), latRefRad_(0.0), lonRefRad_(0.0),
      metersPerRadLat_(0.0), metersPerRadLon_(0.0) {}

bool GeofenceEngine::setThresholds(double veryNearM, double nearM) {
  if (veryNearM < 0.0 || nearM < 0.0 || veryNearM > nearM) return false;
  veryNearM_ = veryNearM;
  nearM_ = nearM;
  return true;
}

bool GeofenceEngine::setFence(const CoordinateE7* points, uint8_t count,
                              uint16_t fenceVersion) {
  if (!points || count < 3 || count > MAX_VERTICES) {
    valid_ = false;
    return false;
  }

  count_ = count;
  fenceVersion_ = fenceVersion;

  double latSum = 0.0;
  double lonSum = 0.0;
  for (uint8_t i = 0; i < count_; ++i) {
    geo_[i] = points[i];
    latSum += e7ToDegrees(points[i].lat);
    lonSum += e7ToDegrees(points[i].lon);
  }

  const double latRefDeg = latSum / count_;
  const double lonRefDeg = lonSum / count_;
  latRefRad_ = latRefDeg * DEG_TO_RAD_D;
  lonRefRad_ = lonRefDeg * DEG_TO_RAD_D;

  const double s = std::sin(latRefRad_);
  const double denom = std::sqrt(1.0 - WGS84_E2 * s * s);
  const double M = WGS84_A * (1.0 - WGS84_E2) /
                   (denom * denom * denom);
  const double N = WGS84_A / denom;

  metersPerRadLat_ = M;
  metersPerRadLon_ = N * std::cos(latRefRad_);

  for (uint8_t i = 0; i < count_; ++i) {
    xy_[i] = toXY(geo_[i]);
  }

  valid_ = true;
  return true;
}

GeofenceEngine::XY GeofenceEngine::toXY(const CoordinateE7& p) const {
  const double latRad = e7ToDegrees(p.lat) * DEG_TO_RAD_D;
  const double lonRad = e7ToDegrees(p.lon) * DEG_TO_RAD_D;
  XY result;
  result.x = (lonRad - lonRefRad_) * metersPerRadLon_;
  result.y = (latRad - latRefRad_) * metersPerRadLat_;
  return result;
}

bool GeofenceEngine::pointInside(const XY& p) const {
  bool inside = false;
  uint8_t j = static_cast<uint8_t>(count_ - 1);

  for (uint8_t i = 0; i < count_; ++i) {
    const XY& pi = xy_[i];
    const XY& pj = xy_[j];

    const bool yStraddles = ((pi.y > p.y) != (pj.y > p.y));
    if (yStraddles) {
      const double xIntersection =
          (pj.x - pi.x) * (p.y - pi.y) / (pj.y - pi.y) + pi.x;
      if (p.x < xIntersection) inside = !inside;
    }
    j = i;
  }

  return inside;
}

double GeofenceEngine::pointSegmentDistance(const XY& p, const XY& a, const XY& b) {
  const double dx = b.x - a.x;
  const double dy = b.y - a.y;
  const double len2 = dx * dx + dy * dy;

  if (len2 <= 1e-12) return std::hypot(p.x - a.x, p.y - a.y);

  double t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2;
  if (t < 0.0) t = 0.0;
  if (t > 1.0) t = 1.0;

  const double nx = a.x + t * dx;
  const double ny = a.y + t * dy;
  return std::hypot(p.x - nx, p.y - ny);
}

double GeofenceEngine::distanceToBorder(const XY& p) const {
  double best = 1.0e100;
  for (uint8_t i = 0; i < count_; ++i) {
    const uint8_t next = static_cast<uint8_t>((i + 1) % count_);
    const double d = pointSegmentDistance(p, xy_[i], xy_[next]);
    if (d < best) best = d;
  }
  return best;
}

GeofenceResult GeofenceEngine::analyze(const CoordinateE7& position) const {
  GeofenceResult result{false, false, 0.0, ProximityState::FAR};
  if (!valid_) return result;

  const XY p = toXY(position);
  const double distance = distanceToBorder(p);
  bool inside = pointInside(p);

  // Treat practically-on-the-border as inside to remove a geometric ambiguity.
  if (distance < 0.05) inside = true;

  ProximityState proximity = ProximityState::FAR;
  if (distance <= veryNearM_) proximity = ProximityState::VERY_NEAR;
  else if (distance <= nearM_) proximity = ProximityState::NEAR;

  result.valid = true;
  result.inside = inside;
  result.distanceBorderM = distance;
  result.proximity = proximity;
  return result;
}

} // namespace vfence
