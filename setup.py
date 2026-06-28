from setuptools import setup, find_packages

setup(
    name="dataset-sync",
    version="0.1.0",
    description="Multi-modal synchronized dataset for rPPG signal prediction using Camera, Oximeter (Contec CMS60D), and WiFi CSI (ESP32)",
    author="Jarvis",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    install_requires=[
        "numpy",
        "pandas",
        "opencv-python",
        "pyserial",
        "torch",
        "pyyaml",
        "tqdm",
        "click",
    ],
)
