import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const el = document.getElementById('viewport');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x111827);
const camera = new THREE.PerspectiveCamera(50, el.clientWidth / el.clientHeight, 0.1, 500);
camera.position.set(3, 2.4, 4);
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setSize(el.clientWidth, el.clientHeight);
el.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 1, 0);
scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const dir = new THREE.DirectionalLight(0xffffff, 1.2);
dir.position.set(4, 8, 3);
scene.add(dir);
scene.add(new THREE.GridHelper(20, 20, 0x475569, 0x1e293b));
const box = new THREE.Mesh(
  new THREE.BoxGeometry(1, 1, 1),
  new THREE.MeshStandardMaterial({ color: 0xf59e0b }));
box.position.y = 0.5;
scene.add(box);
function tick() { requestAnimationFrame(tick); controls.update(); renderer.render(scene, camera); }
tick();
window.addEventListener('resize', () => {
  camera.aspect = el.clientWidth / el.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(el.clientWidth, el.clientHeight);
});
