# Changelog

## v1.0.0 - First release

- Record selected controls' world-space motion into an EP curve (batched
  sampling across all controls in a single timeline scrub). Held poses
  (repeated consecutive positions) are collapsed before fitting, since
  repeated points can make EP fitting fail outright; falls back to a CV
  curve automatically if EP fitting still fails for any other reason.
- Mstr_Loc / Sub_LocOffset_Grp / Sub_Loc rig travels the curve; rotation is
  baked decoupled from the curve's tangent orientation by default, with an
  optional Follow Path Angle mode to inherit yaw only.
- Three frame-range modes: Playback Range, Custom Range, and each
  control's own keyframe range.
- Attach Controls back onto the rig via point/orient constraints, matching
  only the translate/rotate channels each control actually has free.
- Locator Scale slider resizes every M2C_-prefixed locator in the scene.
- Self-contained drag-and-drop installer (embeds the tool script and icon,
  adds a shelf button automatically).
