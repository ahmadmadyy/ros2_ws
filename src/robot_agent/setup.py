from setuptools import find_packages, setup

package_name = 'robot_agent'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', ['launch/agent_bringup.launch.py']),
        ('share/' + package_name + '/config', ['config/agent_params.yaml']),
    ],
    install_requires=[
        'setuptools',
        'fastapi',
        'uvicorn',
        'httpx',
    ],
    zip_safe=True,
    entry_points={
        'console_scripts': [
            'robot_agent = robot_agent.main:main',
        ],
    },
)
