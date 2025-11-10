from setuptools import setup, find_packages

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(
    name='plastic-app',

    # The version of your package
    version='0.1.0',

    packages=find_packages(),

    include_package_data=True,

    install_requires=requirements,

    entry_points={
        'console_scripts': [
            'plastic-app=plastic_app.app:app',
        ],
    },

    author="Vectorplasticity",
    description="Visualize your app files.",
    url="https://github.com/vectorplasticity/Plastic-App",
    python_requires='>=3.6',
)
