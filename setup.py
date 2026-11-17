from setuptools import find_namespace_packages, setup

setup(
    name="codereins",
    version="0.1.0",
    description="Drive Visual Studio Code and editors built on it from the command line: windows, "
                "editors, language features, terminals, tasks, debugging, extensions and settings.",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/Kapildevv/codereins",
    license="Apache-2.0",
    packages=find_namespace_packages(include=["codereins", "codereins.*"]),
    package_data={
        "codereins": ["skills/*.md", "bridge/*", "README.md"],
        "codereins.bridge": ["*"],
        "codereins.skills": ["*.md"],
        "codereins.tests": ["*.md"],
    },
    install_requires=[
        "click>=8.0.0",
        "prompt-toolkit>=3.0.0",
    ],
    extras_require={"test": ["pytest>=8.0.0"]},
    entry_points={
        "console_scripts": [
            "codereins=codereins.vscode_cli:main",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Environment :: Console",
        "Topic :: Software Development",
    ],
)
