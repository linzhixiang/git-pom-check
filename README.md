## 1、效果如图所属
<img width="1307" height="314" alt="image" src="https://github.com/user-attachments/assets/981aa0be-ce5a-41d5-bd24-d56d31efc1b9" />
<img width="1481" height="700" alt="image" src="https://github.com/user-attachments/assets/fee1bdbf-18c4-4616-82e7-c70ea5f87bc5" />


## 2、项目部署/启动
#先调整main.py，修改git-token 用于访问git项目

pip install -r requirements.txt
#设置本地的maven仓库
$env:M2_REPO = "Z:\repo5"
$env:MAVEN_FALLBACK_ON_FAILURE = "false"
uvicorn app.main:app --host 172.31.45.101 --port 8000


## 3、接口文档
### 接口1：批量解析gitlab项目依赖	
http://localhost:8000/api/v1/analyze/gitlab/batch
请求类型：post	
```
{
  "projects": [
    {
      "repo_url": "https://｛git地址1｝.git",
      "branch": "master"
    },
    {
      "repo_url": "https://｛git地址2｝.git",
      "branch": "master"
    }
  ]
}
```

接口返回示例：	
```
{
    "task_id": "179b7e6a51ae4bfb8db970298a0802d9",
    "status": "pending",
    "project_count": 2,
    "task_url": "/api/v1/tasks/179b7e6a51ae4bfb8db970298a0802d9",
    "excel_url": "/api/v1/tasks/179b7e6a51ae4bfb8db970298a0802d9/excel"
}
```
	
	
### 接口2：获取任务解析进度	
http://localhost:8000/api/v1/tasks/{task_id}

请求类型：get	补充接口1返回的task_id到url，获取异步处理任务进度
	
	
### 接口3：下载解析后的项目依赖	
http://localhost:8000/api/v1/tasks/{task_id}/excel 

请求类型：get	地址复制到浏览器，返回处理后的文档
	
