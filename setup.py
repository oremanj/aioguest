from setuptools import setup, find_packages

exec(open("aioguest/_version.py", encoding="utf-8").read())

LONG_DESC = open("README.rst", encoding="utf-8").read()

setup(
    name="aioguest",
    version=__version__,
    description="Run asyncio and another event loop in the same thread",
    url="https://github.com/oremanj/aioguest",
    long_description=LONG_DESC,
    author="Joshua Oreman",
    author_email="oremanj@gmail.com",
    license="MIT -or- Apache License 2.0",
    packages=find_packages(),
    install_requires=[
        "attrs >= 21.3.0",
        "greenlet",
        "trio >= 0.16.0",
        "sniffio >= 1.3.0",
        "outcome",
    ]
    include_package_data=True,
    keywords=["async", "trio", "asyncio"],
    python_requires=">=3.7",
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
        "Development Status :: 2 - Pre-Alpha",
        "Intended Audience :: Developers",
    ],
)
