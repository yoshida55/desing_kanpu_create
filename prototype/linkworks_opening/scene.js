import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.185.1/build/three.module.js";

const canvas = document.querySelector("[data-scene]");
const intro = document.querySelector("[data-intro]");

if (!canvas || !intro) {
  throw new Error("3D scene target was not found.");
}

const colors = {
  night: 0x020908,
  green: 0x49f2bb,
  acid: 0xd9ff79,
  orange: 0xff8a5c,
  deepGreen: 0x0b2c27,
  white: 0xffffff
};

const scene = new THREE.Scene();
scene.background = new THREE.Color(colors.night);
scene.fog = new THREE.FogExp2(colors.night, 0.025);

const camera = new THREE.PerspectiveCamera(47, window.innerWidth / window.innerHeight, 0.1, 120);
camera.position.set(0, 0.7, 11.6);

const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  powerPreference: "high-performance"
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.18;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const ambientLight = new THREE.HemisphereLight(colors.acid, colors.deepGreen, 1.35);
scene.add(ambientLight);

const keyLight = new THREE.DirectionalLight(colors.white, 2.8);
keyLight.position.set(-4, 8, 8);
keyLight.castShadow = true;
keyLight.shadow.mapSize.set(1024, 1024);
scene.add(keyLight);

const portalLight = new THREE.PointLight(colors.green, 85, 34, 1.8);
portalLight.position.set(0, 0, 2);
scene.add(portalLight);

const craftLight = new THREE.PointLight(colors.orange, 62, 28, 1.7);
craftLight.position.set(6, 1, -13);
scene.add(craftLight);

const digitalLight = new THREE.PointLight(colors.green, 72, 30, 1.7);
digitalLight.position.set(-6, 1, -13);
scene.add(digitalLight);

const world = new THREE.Group();
scene.add(world);

function createBox(width, height, depth, material, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, height, depth), material);
  mesh.position.set(x, y, z);
  return mesh;
}

function createTube(points, radius, material) {
  const curve = new THREE.CatmullRomCurve3(points);
  const geometry = new THREE.TubeGeometry(curve, 64, radius, 8, false);
  return { mesh: new THREE.Mesh(geometry, material), curve };
}

function createGlowSprite(color, scale) {
  const glowCanvas = document.createElement("canvas");
  glowCanvas.width = 128;
  glowCanvas.height = 128;
  const context = glowCanvas.getContext("2d");
  const gradient = context.createRadialGradient(64, 64, 0, 64, 64, 64);
  const colorValue = new THREE.Color(color).getStyle();
  gradient.addColorStop(0, colorValue);
  gradient.addColorStop(0.2, colorValue.replace("rgb", "rgba").replace(")", ", 0.7)"));
  gradient.addColorStop(1, "rgba(0, 0, 0, 0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);

  const texture = new THREE.CanvasTexture(glowCanvas);
  const material = new THREE.SpriteMaterial({
    map: texture,
    color,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(scale, scale, 1);
  return sprite;
}

/* 扉とフレーム */
const frameMaterial = new THREE.MeshPhysicalMaterial({
  color: 0x102b25,
  metalness: 0.82,
  roughness: 0.24,
  emissive: 0x071713,
  emissiveIntensity: 0.8
});

const doorMaterial = new THREE.MeshPhysicalMaterial({
  color: 0x091b17,
  metalness: 0.68,
  roughness: 0.28,
  clearcoat: 0.55,
  emissive: 0x071a15,
  emissiveIntensity: 0.9
});

const lineMaterial = new THREE.MeshBasicMaterial({ color: colors.green });
const architectureLineMaterial = new THREE.MeshBasicMaterial({
  color: colors.green,
  transparent: true,
  opacity: 0.38
});
const handleMaterial = new THREE.MeshPhysicalMaterial({
  color: colors.acid,
  metalness: 0.9,
  roughness: 0.18,
  emissive: colors.green,
  emissiveIntensity: 0.7
});

const portalFrame = new THREE.Group();
scene.add(portalFrame);

portalFrame.add(createBox(0.34, 9.2, 0.72, frameMaterial, -3.42, 0, 0));
portalFrame.add(createBox(0.34, 9.2, 0.72, frameMaterial, 3.42, 0, 0));
portalFrame.add(createBox(7.18, 0.36, 0.72, frameMaterial, 0, 4.43, 0));

const leftHinge = new THREE.Group();
leftHinge.position.set(-3.22, 0, 0.1);
const leftDoor = createBox(3.18, 8.55, 0.25, doorMaterial, 1.59, 0, 0);
leftDoor.castShadow = true;
leftHinge.add(leftDoor);
leftHinge.add(createBox(0.035, 7.1, 0.28, lineMaterial, 2.78, 0, 0.14));
const leftHandle = createBox(0.12, 0.82, 0.22, handleMaterial, 2.94, -0.15, 0.24);
leftHinge.add(leftHandle);
portalFrame.add(leftHinge);

const rightHinge = new THREE.Group();
rightHinge.position.set(3.22, 0, 0.1);
const rightDoor = createBox(3.18, 8.55, 0.25, doorMaterial, -1.59, 0, 0);
rightDoor.castShadow = true;
rightHinge.add(rightDoor);
rightHinge.add(createBox(0.035, 7.1, 0.28, lineMaterial, -2.78, 0, 0.14));
const rightHandle = createBox(0.12, 0.82, 0.22, handleMaterial, -2.94, -0.15, 0.24);
rightHinge.add(rightHandle);
portalFrame.add(rightHinge);

const doorPanelMaterial = new THREE.MeshBasicMaterial({
  color: colors.green,
  transparent: true,
  opacity: 0.14,
  side: THREE.DoubleSide
});

function addDoorPanels(hinge, direction) {
  const panelGeometry = new THREE.RingGeometry(0.72, 0.74, 4);
  const panel = new THREE.Mesh(panelGeometry, doorPanelMaterial);
  panel.position.set(direction * 0.7, 1.8, 0.14);
  panel.rotation.z = Math.PI / 4;
  hinge.add(panel);

  const secondPanel = panel.clone();
  secondPanel.position.y = -1.8;
  secondPanel.scale.setScalar(1.4);
  hinge.add(secondPanel);
}

addDoorPanels(leftHinge, 1);
addDoorPanels(rightHinge, -1);

const portalMaterial = new THREE.MeshBasicMaterial({
  color: colors.green,
  transparent: true,
  opacity: 0.12,
  side: THREE.DoubleSide
});
const portalGlow = new THREE.Mesh(new THREE.PlaneGeometry(6.3, 8.4), portalMaterial);
portalGlow.position.z = -0.3;
scene.add(portalGlow);

const thresholdGlow = createGlowSprite(colors.green, 10);
thresholdGlow.position.set(0, 0, -0.6);
scene.add(thresholdGlow);

/* 扉へ歩き、自分の手で開く人 */
const humanMaterial = new THREE.MeshStandardMaterial({
  color: 0x030706,
  metalness: 0,
  roughness: 0.88,
  emissive: 0x06120f,
  emissiveIntensity: 0.22
});
const humanAccentMaterial = new THREE.MeshBasicMaterial({ color: colors.green });
const human = new THREE.Group();
human.position.set(0, -4.08, 4.1);
scene.add(human);

const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.53, 1.45, 8, 16), humanMaterial);
torso.position.y = 2.05;
torso.castShadow = true;
human.add(torso);

const head = new THREE.Mesh(new THREE.SphereGeometry(0.46, 24, 18), humanMaterial);
head.position.y = 3.65;
head.castShadow = true;
human.add(head);

const neckLight = createBox(0.54, 0.035, 0.55, humanAccentMaterial, 0, 3.13, 0.02);
human.add(neckLight);

function createLimb(length, radius) {
  const pivot = new THREE.Group();
  const limb = new THREE.Mesh(new THREE.CapsuleGeometry(radius, length - radius * 2, 6, 12), humanMaterial);
  limb.position.y = -length / 2;
  limb.castShadow = true;
  pivot.add(limb);
  return pivot;
}

const leftArm = createLimb(1.55, 0.19);
leftArm.position.set(-0.62, 2.74, 0);
const leftHand = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 12), humanMaterial);
leftHand.position.y = -1.55;
leftArm.add(leftHand);
human.add(leftArm);

const rightArm = createLimb(1.55, 0.19);
rightArm.position.set(0.62, 2.74, 0);
const rightHand = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 12), humanMaterial);
rightHand.position.y = -1.55;
rightArm.add(rightHand);
human.add(rightArm);

const leftLeg = createLimb(1.7, 0.23);
leftLeg.position.set(-0.29, 1.2, 0);
human.add(leftLeg);

const rightLeg = createLimb(1.7, 0.23);
rightLeg.position.set(0.29, 1.2, 0);
human.add(rightLeg);

const humanRim = new THREE.PointLight(colors.green, 25, 8, 1.8);
humanRim.position.set(0, 2.2, 1.2);
human.add(humanRim);

/* 奥へ続く、層を持った仕事の都市 */
const grid = new THREE.GridHelper(60, 60, colors.green, 0x113c33);
grid.position.set(0, -4.2, -22);
grid.material.transparent = true;
grid.material.opacity = 0.34;
world.add(grid);

const platformMaterial = new THREE.MeshPhysicalMaterial({
  color: 0x0b211c,
  metalness: 0.72,
  roughness: 0.34,
  emissive: 0x061c17,
  emissiveIntensity: 0.7
});

const centerWalkway = createBox(5.2, 0.22, 29, platformMaterial, 0, -3.95, -14);
centerWalkway.receiveShadow = true;
world.add(centerWalkway);
world.add(createBox(0.05, 0.05, 28, lineMaterial, -2.25, -3.8, -14));
world.add(createBox(0.05, 0.05, 28, lineMaterial, 2.25, -3.8, -14));

for (let index = 0; index < 7; index += 1) {
  const depth = -5 - index * 4.2;
  const frameScale = 1 + index * 0.13;
  const arch = new THREE.Group();
  arch.add(createBox(0.08, 7 * frameScale, 0.08, architectureLineMaterial, -5.1 * frameScale, -0.6, 0));
  arch.add(createBox(0.08, 7 * frameScale, 0.08, architectureLineMaterial, 5.1 * frameScale, -0.6, 0));
  arch.add(createBox(10.2 * frameScale, 0.08, 0.08, architectureLineMaterial, 0, 2.9 * frameScale, 0));
  arch.position.z = depth;
  world.add(arch);
}

const ringMaterial = new THREE.MeshBasicMaterial({
  color: colors.green,
  transparent: true,
  opacity: 0.74
});
const orangeRingMaterial = ringMaterial.clone();
orangeRingMaterial.color = new THREE.Color(colors.orange);

const outerRing = new THREE.Mesh(new THREE.TorusGeometry(6.2, 0.04, 8, 160), ringMaterial);
outerRing.position.set(0, 0.4, -22);
outerRing.rotation.x = 0.25;
world.add(outerRing);

const middleRing = new THREE.Mesh(new THREE.TorusGeometry(4.4, 0.06, 8, 128), orangeRingMaterial);
middleRing.position.set(0, 0.4, -22);
middleRing.rotation.set(0.25, 0.58, 0);
world.add(middleRing);

/* まだ名前のない可能性が枝分かれする発光樹 */
const possibilityTree = new THREE.Group();
possibilityTree.position.set(0, 0, -22);
world.add(possibilityTree);

const branchMaterial = new THREE.MeshBasicMaterial({ color: colors.acid });
const trunkPoints = [
  new THREE.Vector3(0, -3.8, 0),
  new THREE.Vector3(-0.25, -2.2, 0.2),
  new THREE.Vector3(0.35, -0.7, -0.1),
  new THREE.Vector3(0, 1.15, 0)
];
const trunk = createTube(trunkPoints, 0.11, branchMaterial);
possibilityTree.add(trunk.mesh);

const nodeMaterial = new THREE.MeshPhysicalMaterial({
  color: colors.acid,
  emissive: colors.green,
  emissiveIntensity: 3.2,
  roughness: 0.14,
  metalness: 0.16
});
const possibilityNodes = [];
const branchEnds = [
  new THREE.Vector3(-2.7, 2.2, 0.3),
  new THREE.Vector3(-1.2, 3.45, -0.3),
  new THREE.Vector3(1.25, 3.55, 0.15),
  new THREE.Vector3(2.8, 2.05, -0.25),
  new THREE.Vector3(0, 4.4, 0)
];

branchEnds.forEach((end, index) => {
  const start = new THREE.Vector3(0, 0.2 + index * 0.18, 0);
  const middle = new THREE.Vector3(end.x * 0.45, end.y * 0.55, end.z + (index % 2 ? -0.5 : 0.5));
  const branch = createTube([start, middle, end], 0.055, branchMaterial);
  possibilityTree.add(branch.mesh);

  const node = new THREE.Mesh(new THREE.IcosahedronGeometry(index === 4 ? 0.72 : 0.48, 2), nodeMaterial);
  node.position.copy(end);
  possibilityNodes.push(node);
  possibilityTree.add(node);
});

const treeGlow = createGlowSprite(colors.green, 11);
treeGlow.position.set(0, 1.3, -0.5);
possibilityTree.add(treeGlow);

/* 左側のITブース */
function createMonitor() {
  const group = new THREE.Group();
  const shellMaterial = new THREE.MeshPhysicalMaterial({
    color: 0x132a26,
    metalness: 0.68,
    roughness: 0.24
  });
  const screenMaterial = new THREE.MeshBasicMaterial({ color: 0x54ffc4 });
  const screen = createBox(3.2, 2, 0.22, shellMaterial, 0, 0.5, 0);
  const face = createBox(2.82, 1.6, 0.025, screenMaterial, 0, 0.5, 0.13);
  const stand = createBox(0.28, 1, 0.28, shellMaterial, 0, -0.9, 0);
  const foot = createBox(1.4, 0.18, 0.9, shellMaterial, 0, -1.35, 0);
  group.add(screen, face, stand, foot);

  const codeMaterial = new THREE.MeshBasicMaterial({ color: colors.deepGreen });
  for (let index = 0; index < 5; index += 1) {
    const bar = createBox(1.9 - index * 0.23, 0.08, 0.03, codeMaterial, -0.25 + index * 0.1, 1.02 - index * 0.28, 0.16);
    group.add(bar);
  }

  return group;
}

const digitalHabitat = new THREE.Group();
digitalHabitat.position.set(-6.1, -1.45, -12.8);
digitalHabitat.rotation.y = 0.34;
world.add(digitalHabitat);

const digitalPlatform = createBox(7.5, 0.32, 6, platformMaterial, 0, -2.2, 0);
digitalHabitat.add(digitalPlatform);
const desk = createBox(5.6, 0.24, 1.8, frameMaterial, 0, -0.75, 0.2);
digitalHabitat.add(desk);
const monitor = createMonitor();
monitor.position.set(0, 0.75, 0);
digitalHabitat.add(monitor);

const hologramMaterial = new THREE.MeshBasicMaterial({
  color: colors.green,
  transparent: true,
  opacity: 0.22,
  side: THREE.DoubleSide,
  blending: THREE.AdditiveBlending
});
const holograms = [];
for (let index = 0; index < 3; index += 1) {
  const hologram = new THREE.Mesh(new THREE.PlaneGeometry(1.7 + index * 0.25, 2.4), hologramMaterial.clone());
  hologram.position.set(-3 + index * 3, 1.2 + index * 0.35, -1.6 - index * 0.65);
  hologram.rotation.y = -0.22 + index * 0.22;
  holograms.push(hologram);
  digitalHabitat.add(hologram);
}

const digitalGlow = createGlowSprite(colors.green, 7);
digitalGlow.position.set(0, 0.5, -1);
digitalHabitat.add(digitalGlow);

/* 右側の梱包・軽作業ライン */
const craftHabitat = new THREE.Group();
craftHabitat.position.set(6.3, -1.5, -13.6);
craftHabitat.rotation.y = -0.28;
world.add(craftHabitat);

const craftPlatform = createBox(7.8, 0.32, 7.5, platformMaterial, 0, -2.15, 0);
craftHabitat.add(craftPlatform);

const beltMaterial = new THREE.MeshPhysicalMaterial({
  color: 0x233e38,
  metalness: 0.72,
  roughness: 0.34
});
const belt = createBox(3.2, 0.38, 7.2, beltMaterial, 0, -0.45, 0);
craftHabitat.add(belt);

for (let index = 0; index < 7; index += 1) {
  craftHabitat.add(createBox(3.25, 0.05, 0.12, lineMaterial, 0, -0.22, -3 + index));
}

const packageMaterial = new THREE.MeshPhysicalMaterial({
  color: colors.orange,
  metalness: 0.12,
  roughness: 0.48
});
const bandMaterial = new THREE.MeshBasicMaterial({ color: colors.acid });
const movingPackages = [];

for (let index = 0; index < 4; index += 1) {
  const packageGroup = new THREE.Group();
  const size = 0.9 + index * 0.12;
  packageGroup.add(createBox(size, size, size, packageMaterial));
  packageGroup.add(createBox(size * 1.02, size * 0.12, size * 1.02, bandMaterial));
  packageGroup.position.set(index % 2 ? 0.45 : -0.45, 0.25, -3 + index * 1.8);
  movingPackages.push(packageGroup);
  craftHabitat.add(packageGroup);
}

const sorterRing = new THREE.Mesh(new THREE.TorusGeometry(2.2, 0.09, 10, 80), orangeRingMaterial);
sorterRing.position.set(0, 1.45, -1.7);
sorterRing.rotation.y = Math.PI / 2;
craftHabitat.add(sorterRing);

const craftGlow = createGlowSprite(colors.orange, 7);
craftGlow.position.set(0, 0.3, -1);
craftHabitat.add(craftGlow);

/* 遠景の塔と、世界を横断する光の軌道 */
const skylineMaterial = new THREE.MeshBasicMaterial({
  color: colors.green,
  transparent: true,
  opacity: 0.18,
  wireframe: true
});

for (let index = 0; index < 13; index += 1) {
  const height = 4 + (index % 5) * 1.4;
  const tower = createBox(1.5 + (index % 3) * 0.45, height, 1.5, skylineMaterial, (index - 6) * 3.4, -4.2 + height / 2, -33 - (index % 3) * 2.8);
  world.add(tower);
}

const energyMaterial = new THREE.MeshBasicMaterial({ color: colors.acid });
const energyPath = createTube([
  new THREE.Vector3(0, -3.75, 0),
  new THREE.Vector3(-1.6, -2.9, -5),
  new THREE.Vector3(1.4, -2.2, -10),
  new THREE.Vector3(-0.8, -1.1, -16),
  new THREE.Vector3(0, 0.6, -21)
], 0.045, energyMaterial);
world.add(energyPath.mesh);

const energyPulse = new THREE.Mesh(new THREE.SphereGeometry(0.17, 16, 12), nodeMaterial);
world.add(energyPulse);

const ribbonOne = createTube([
  new THREE.Vector3(-11, 2.5, -15),
  new THREE.Vector3(-5, 5.5, -20),
  new THREE.Vector3(2, 4.5, -24),
  new THREE.Vector3(10, 1.8, -27)
], 0.035, ringMaterial);
world.add(ribbonOne.mesh);

const ribbonTwo = createTube([
  new THREE.Vector3(11, -0.5, -17),
  new THREE.Vector3(5, 3.8, -21),
  new THREE.Vector3(-1, 5.2, -25),
  new THREE.Vector3(-10, 1.4, -29)
], 0.035, orangeRingMaterial);
world.add(ribbonTwo.mesh);

const particleCount = 760;
const particlePositions = new Float32Array(particleCount * 3);

for (let index = 0; index < particleCount; index += 1) {
  const radius = 5 + Math.random() * 28;
  const angle = Math.random() * Math.PI * 2;
  particlePositions[index * 3] = Math.cos(angle) * radius;
  particlePositions[index * 3 + 1] = (Math.random() - 0.5) * 20;
  particlePositions[index * 3 + 2] = -5 - Math.sin(angle) * radius - Math.random() * 24;
}

const particleGeometry = new THREE.BufferGeometry();
particleGeometry.setAttribute("position", new THREE.BufferAttribute(particlePositions, 3));
const particleMaterial = new THREE.PointsMaterial({
  color: colors.green,
  size: 0.055,
  transparent: true,
  opacity: 0.72,
  blending: THREE.AdditiveBlending
});
const particles = new THREE.Points(particleGeometry, particleMaterial);
world.add(particles);

let startTime = performance.now();
let isRunning = false;

function clamp(value, minimum = 0, maximum = 1) {
  return Math.min(Math.max(value, minimum), maximum);
}

function easeInOut(value) {
  return value < 0.5 ? 4 * value * value * value : 1 - Math.pow(-2 * value + 2, 3) / 2;
}

function updateScene(now) {
  const elapsed = (now - startTime) / 5000;
  const walkProgress = easeInOut(clamp(elapsed / 0.24));
  const reachProgress = easeInOut(clamp((elapsed - 0.16) / 0.16));
  const doorProgress = easeInOut(clamp((elapsed - 0.28) / 0.32));
  const cameraProgress = easeInOut(clamp((elapsed - 0.43) / 0.54));
  const worldProgress = easeInOut(clamp((elapsed - 0.5) / 0.38));
  const pulse = now * 0.001;
  const walkingCycle = Math.sin(walkProgress * Math.PI * 5);

  human.position.z = THREE.MathUtils.lerp(4.1, 1.2, walkProgress);
  human.position.y = -4.08 + Math.abs(walkingCycle) * 0.05 * (1 - reachProgress);
  torso.rotation.x = THREE.MathUtils.lerp(0.04, 0.15, reachProgress);
  head.rotation.y = Math.sin(walkProgress * Math.PI) * 0.08;
  leftLeg.rotation.x = walkingCycle * 0.62 * (1 - reachProgress);
  rightLeg.rotation.x = -walkingCycle * 0.62 * (1 - reachProgress);
  leftArm.rotation.x = THREE.MathUtils.lerp(-walkingCycle * 0.38, 1.42, reachProgress);
  rightArm.rotation.x = THREE.MathUtils.lerp(walkingCycle * 0.38, 1.42, reachProgress);
  leftArm.rotation.z = THREE.MathUtils.lerp(0.16, -0.78, doorProgress);
  rightArm.rotation.z = THREE.MathUtils.lerp(-0.16, 0.78, doorProgress);

  leftHinge.rotation.y = doorProgress * -1.9;
  rightHinge.rotation.y = doorProgress * 1.9;
  human.visible = cameraProgress < 0.72;

  camera.position.z = THREE.MathUtils.lerp(11.6, -6.6, cameraProgress);
  camera.position.y = THREE.MathUtils.lerp(0.7, 0.35, cameraProgress);
  camera.position.x = Math.sin(cameraProgress * Math.PI) * 0.42;
  camera.lookAt(
    0,
    THREE.MathUtils.lerp(-0.7, 0.4, cameraProgress),
    THREE.MathUtils.lerp(-1.5, -19.5, cameraProgress)
  );

  portalGlow.material.opacity = THREE.MathUtils.lerp(0.05, 0.1, doorProgress) * (1 - cameraProgress * 0.94);
  thresholdGlow.material.opacity = THREE.MathUtils.lerp(0.22, 0.46, doorProgress) * (1 - cameraProgress * 0.82);
  thresholdGlow.scale.setScalar(THREE.MathUtils.lerp(8, 14, doorProgress));
  portalLight.intensity = THREE.MathUtils.lerp(36, 118, doorProgress);

  world.scale.setScalar(THREE.MathUtils.lerp(0.9, 1, worldProgress));
  outerRing.rotation.z = pulse * 0.28;
  middleRing.rotation.z = -pulse * 0.42;
  possibilityTree.rotation.y = Math.sin(pulse * 0.42) * 0.08;
  possibilityNodes.forEach((node, index) => {
    node.rotation.x = pulse * (0.18 + index * 0.025);
    node.rotation.y = pulse * (0.24 + index * 0.03);
    node.scale.setScalar(1 + Math.sin(pulse * 1.8 + index) * 0.06);
  });

  monitor.position.y = 0.75 + Math.sin(pulse * 1.6) * 0.15;
  holograms.forEach((hologram, index) => {
    hologram.material.opacity = 0.17 + Math.sin(pulse * 2 + index) * 0.045;
  });
  sorterRing.rotation.z = pulse * 0.7;
  movingPackages.forEach((packageGroup, index) => {
    packageGroup.position.z = 3.2 - ((pulse * 0.9 + index * 1.8) % 7.2);
    packageGroup.rotation.y = Math.sin(pulse + index) * 0.08;
  });

  energyPulse.position.copy(energyPath.curve.getPoint((pulse * 0.12) % 1));
  particles.rotation.y = pulse * 0.018;

  renderer.render(scene, camera);
}

function startScene() {
  startTime = performance.now();
  isRunning = true;
  camera.position.set(0, 0.7, 11.6);
  human.position.set(0, -4.08, 4.1);
  human.visible = true;
  leftHinge.rotation.y = 0;
  rightHinge.rotation.y = 0;
  renderer.setAnimationLoop(updateScene);
}

function stopScene() {
  isRunning = false;
  renderer.setAnimationLoop(null);
}

function resizeScene() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.setSize(window.innerWidth, window.innerHeight);

  if (!isRunning) {
    renderer.render(scene, camera);
  }
}

window.addEventListener("resize", resizeScene);
window.addEventListener("linkworks:intro-start", startScene);
window.addEventListener("linkworks:intro-finish", stopScene);

intro.classList.add("has_webgl");
window.dispatchEvent(new CustomEvent("linkworks:scene-ready"));
