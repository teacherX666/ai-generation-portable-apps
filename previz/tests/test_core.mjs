import test from 'node:test';
import assert from 'node:assert/strict';
import {
  JOINTS, POSE_PRESETS, jointAngles, ASPECTS, SHOT_SIZES, shotDistance,
  PROP_TYPES, CHARACTER_COLORS, newCharacter, newShot, emptyProject,
  renderSizeFor, sanitizeFilename, newId, dataUrlToBlob, obbPenetration,
} from '../static/core.js';

test('JOINTS 恰好 14 个且无重复', () => {
  assert.equal(JOINTS.length, 14);
  assert.equal(new Set(JOINTS).size, 14);
});

test('POSE_PRESETS 每个姿势都覆盖全部 14 关节', () => {
  for (const [name, angles] of Object.entries(POSE_PRESETS)) {
    for (const j of JOINTS) {
      assert.ok(Array.isArray(angles[j]), `${name}.${j} 应为 [x,y,z]`);
      assert.equal(angles[j].length, 3, `${name}.${j} 长度应为 3`);
    }
  }
  assert.ok(POSE_PRESETS['站立']);
});

test('jointAngles 合并偏移量且偏移覆盖预设', () => {
  const merged = jointAngles('站立', { left_elbow: [0.5, 0, 0] });
  assert.deepEqual(merged['left_elbow'], [0.5, 0, 0]);
  assert.deepEqual(merged['right_elbow'], POSE_PRESETS['站立']['right_elbow']);
});

test('shotDistance 五档 + 未知回退中景', () => {
  assert.equal(shotDistance('远景'), SHOT_SIZES['远景']);
  assert.equal(shotDistance('特写'), SHOT_SIZES['特写']);
  assert.equal(shotDistance('不认识'), SHOT_SIZES['中景']);
});

test('renderSizeFor 横竖画幅 + 宽边取偶数', () => {
  assert.deepEqual(renderSizeFor('16:9', 'default'), { width: 1920, height: 1080 });
  assert.deepEqual(renderSizeFor('9:16', 'hd'), { width: 1440, height: 2560 });
  assert.deepEqual(renderSizeFor('2:3', 'default'), { width: 1280, height: 1920 });
});

test('newCharacter 颜色循环 + 默认值', () => {
  const a = newCharacter('A', '主角');
  const b = newCharacter('B', '配角');
  assert.equal(a.color, CHARACTER_COLORS[0]);
  assert.equal(b.color, CHARACTER_COLORS[1]);
  assert.deepEqual(a.position, [0, 0, 0]);
  assert.equal(a.pose, '站立');
});

test('newShot 默认相机与画幅', () => {
  const s = newShot('镜头1', 0);
  assert.equal(s.camera.fov, 50);
  assert.equal(s.camera.shot_size, '中景');
  assert.equal(s.aspect, '16:9');
  assert.equal(s.order, 0);
});

test('emptyProject 结构 + newId 前缀与唯一性', () => {
  const p = emptyProject('测试');
  assert.ok(p.id.startsWith('p_'));
  assert.deepEqual(p.shots, []);
  assert.notEqual(newId('s_'), newId('s_'));
});

test('sanitizeFilename 去危险字符', () => {
  assert.equal(sanitizeFilename('镜头 1/2：远景'), '镜头_1_2_远景');
});

test('dataUrlToBlob 解析 data URL', () => {
  const blob = dataUrlToBlob('data:image/png;base64,iVBORw0KGgo=');
  assert.equal(blob.type, 'image/png');
  assert.equal(blob.size, 8);   // 8 字节 PNG 签名头
});

test('PROP_TYPES 十类结构完整', () => {
  assert.equal(PROP_TYPES.length, 10);
  for (const p of PROP_TYPES) {
    assert.ok(p.type && p.label);
    assert.equal(p.geo.length, 2);
    assert.ok(Array.isArray(p.geo[1]));
  }
});

test('obbPenetration 轴对齐最小穿透', () => {
  // a 在 b 的 +x 侧（a.x=0.5 > b.x=0），应沿 +x 推出（远离 b）；穿透 0.1
  const a = { x: 0.5, z: 0, hx: 0.3, hz: 0.3, ry: 0 };
  const b = { x: 0, z: 0, hx: 0.3, hz: 0.3, ry: 0 };
  const r = obbPenetration(a, b);
  assert.ok(r && Math.abs(r.pen - 0.1) < 1e-9);
  assert.equal(r.sign, 1);   // a 在 b 的 +x 侧，应沿 +x 推出
});

test('obbPenetration 分离返回 null', () => {
  assert.equal(obbPenetration({ x: 0, z: 0, hx: 0.3, hz: 0.3, ry: 0 },
                              { x: 5, z: 5, hx: 0.3, hz: 0.3, ry: 0 }), null);
});

test('obbPenetration 旋转 90° 墙', () => {
  const wall = { x: 0, z: 0, hx: 1.5, hz: 0.125, ry: Math.PI / 2 };  // 沿 z 长
  const box = { x: 0, z: 1.0, hx: 0.3, hz: 0.3, ry: 0 };
  const r = obbPenetration(box, wall);
  assert.ok(r && r.pen > 0);
  assert.ok(r.ux * r.sign > 0 || r.uz * r.sign > 0);  // 有推出方向
});
