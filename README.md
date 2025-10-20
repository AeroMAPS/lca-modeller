🚀 LCA-Modeller
===============================================================

[![image](https://img.shields.io/pypi/v/lca-modeller.svg)](https://pypi.python.org/pypi/lca-modeller)
[![image](https://img.shields.io/pypi/pyversions/lca-modeller.svg)](https://pypi.python.org/pypi/lca-modeller)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)


*LCA-Modeller* offers a streamlined interface to facilitate the creation of **parametric LCA models** with **prospective capabilities**. It builds on the open-source libraries [*lca-algebraic*](https://lca-algebraic.readthedocs.io/) and [*premise*](https://premise.readthedocs.io/), so having a basic understanding of these tools is recommended.

The core functionality of *LCA-Modeller* revolves around reading a user-provided configuration file that defines the LCA model. From this configuration file, *LCA-Modeller* generates a parametric LCA model with *lca-algebraic*, which can then be evaluated for any parameter values using *lca-algebraic*'s built-in functions.
<br> 
If prospective scenarios are provided, *premise* is used to adapt the EcoInvent database to future conditions. The parametric LCA model then interpolates the prospective databases to enable the evaluation for any year specified by the user.

Additional features include the definition of custom impact assessment methods and the ability to modify existing activities in the EcoInvent database by adding or updating flows.


📦 Installation
----------------
To install *LCA-Modeller*, setup a separate conda environment:
```bash
conda create -n lca_modeller python==3.10
conda activate lca_modeller
```
And pip install the package:
```bash
pip install lca-modeller
```

A tutorial notebook is provided in the `notebooks` directory to help you get started with *LCA-Modeller*.


✈️ Applications
----------------
*LCA-Modeller* is currently being used in the following projects:
* [AeroMAPS](https://github.com/AeroMAPS/AeroMAPS) : Multidisciplinary Assessment of Prospective Scenarios for air transport.
* [FAST-UAV](https://github.com/SizingLab/FAST-UAV): Future Aircraft Sizing Tool - Unmanned Aerial Vehicles
* [FAST-OAD](https://github.com/fast-aircraft-design/FAST-OAD): Future Aircraft Sizing Tool - Overall Aircraft Design


🤝 Questions and contributions
-------------------------
* Félix POLLET [felix.pollet@isae-supaero.fr](felix.pollet@isae-supaero.fr)

